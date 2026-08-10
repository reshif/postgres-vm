"""Plane A ingestion — the `.memory/` tree in a git repository.

ADR-0002 makes the repository the authoritative plane, so this is the only path
that produces `authoritative` memories. Everything it writes carries the exact
commit that last touched the file as source_version, which is what makes
provenance resolvable back to a reviewable diff.

Layout (00-MASTER-BLUEPRINT.md §159):

    .memory/project.yaml       identity, purpose, constraints, stack
    .memory/decisions/*.md     one decision per file, front-mattered
    .memory/procedures/*.md    steps, preconditions, verifications
    .memory/conventions.md     team conventions the agent must follow
    .memory/glossary.md        entities and their canonical names

Three rules the build plan states and this module enforces:

  * ONE FILE = ONE MEMORY, NEVER CHUNKED. A file over the 8000-char column limit
    is REJECTED rather than split. Chunking an ADR would let half a decision be
    retrieved as if it were the whole decision, which is worse than not having
    it: the reader cannot tell that the conclusion is missing.
  * IDEMPOTENT BY CONTENT HASH. Re-running ingestion on an unchanged tree writes
    nothing new (memories.write_memory dedupes), so a poll loop is safe.
  * DELETING A FILE ARCHIVES ITS MEMORY. Archive, never delete: retracting a
    decision is itself part of the record, and Phase 3 has to answer "what did we
    believe in June" (ADR-0006, bi-temporal).

Secrets are scanned BEFORE anything is written, and a hit aborts that file with
no partial state.
"""
from __future__ import annotations

import io
import logging
import re
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import memories, secret_scan

log = logging.getLogger("memory.ingest")

# Directory / filename -> mem.memory_type. Anything unmatched is skipped rather
# than guessed at: a mis-typed memory ranks and filters wrongly forever.
TYPE_BY_DIR: dict[str, str] = {
    "decisions": "decision",
    "procedures": "procedure",
}
TYPE_BY_FILE: dict[str, str] = {
    "conventions.md": "convention",
    "glossary.md": "entity_fact",
    "project.yaml": "constraint",
}


@dataclass
class IngestReport:
    created: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "created": len(self.created), "unchanged": len(self.unchanged),
            "archived": len(self.archived), "rejected": len(self.rejected),
            "skipped": len(self.skipped),
        }


def parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter from the body.

    Hand-rolled rather than pulling in a YAML parser: the frontmatter here is
    flat scalars and simple lists, and a full YAML load on repository content is
    a deserialisation surface pointed straight at attacker-influenced input.
    Unrecognised structure is left as a string instead of being guessed at.
    """
    if not raw.startswith("---"):
        return {}, raw
    end = raw.find("\n---", 3)
    if end == -1:
        return {}, raw
    head, body = raw[3:end], raw[end + 4:]

    meta: dict[str, Any] = {}
    key: str | None = None
    for line in head.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and key:
            meta.setdefault(key, [])
            if isinstance(meta[key], list):
                meta[key].append(stripped[2:].split("#")[0].strip())
            continue
        if ":" in stripped:
            k, _, v = stripped.partition(":")
            key = k.strip()
            v = v.split(" #")[0].strip().strip("'\"")
            meta[key] = v if v else []
    return meta, body.lstrip("\n")


_COMMIT = re.compile(r"^[0-9a-fA-F]{7,64}$")


class CommitSnapshotError(RuntimeError):
    """The requested commit cannot safely supply a Plane A snapshot."""


def git_sha(repo: Path, rel: str, *, ref: str | None = None) -> str | None:
    """Commit that last touched this file — the exact commit Phase 1 acceptance
    asks provenance to resolve to, not merely current HEAD."""
    try:
        args = [
            # -c safe.directory: a bind-mounted checkout is owned by the host
            # user, not the container user, so git refuses it with "detected
            # dubious ownership" and returns nothing. Provenance then silently
            # becomes NULL for every file — the failure is invisible in the data.
            # Scoped to this one invocation rather than written into global git
            # config, so it cannot loosen the check for anything else.
            "git", "-c", f"safe.directory={repo}", "-C", str(repo),
            "log", "-1", "--format=%H",
        ]
        if ref:
            args.append(ref)
        args.extend(["--", rel])
        out = subprocess.run(
            args,
            capture_output=True, text=True, timeout=15, check=False,
        )
        return (out.stdout.strip() or None) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("git sha unavailable for %s: %s", rel, exc)
        return None


@contextmanager
def commit_snapshot(repo_root: Path, sha: str) -> Iterator[Path]:
    """Yield a read-only-style work tree containing `.memory` at one commit.

    A queue can lag behind a push. Reading the live checkout at that point would
    record a later document under the older webhook SHA, which corrupts Plane A
    provenance. `git archive` gives the task exactly the named tree without a
    checkout, reset, or fetch that could touch an operator's working copy.
    """
    if not _COMMIT.fullmatch(sha):
        raise CommitSnapshotError("commit SHA must be 7-64 hexadecimal characters")
    try:
        verified = subprocess.run(
            ["git", "-c", f"safe.directory={repo_root}", "-C", str(repo_root),
             "rev-parse", "--verify", f"{sha}^{{commit}}"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if verified.returncode != 0:
            raise CommitSnapshotError(f"commit {sha} is not available in {repo_root}")
        resolved_sha = verified.stdout.strip()
        archive = subprocess.run(
            ["git", "-c", f"safe.directory={repo_root}", "-C", str(repo_root),
             "archive", "--format=tar", resolved_sha, ".memory"],
            capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CommitSnapshotError(f"could not archive commit {sha}: {exc}") from exc
    if archive.returncode != 0 or not archive.stdout:
        detail = archive.stderr.decode("utf-8", "replace").strip()
        raise CommitSnapshotError(
            f"commit {resolved_sha} has no readable .memory tree" +
            (f": {detail}" if detail else "")
        )

    with tempfile.TemporaryDirectory(prefix="memory-commit-") as directory:
        root = Path(directory)
        try:
            with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
                for member in tar.getmembers():
                    target = Path(member.name)
                    if (target.is_absolute() or ".." in target.parts
                            or target.parts[:1] != (".memory",)):
                        raise CommitSnapshotError("git archive contained an unsafe .memory path")
                    destination = root / target
                    if member.isdir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    if not member.isfile():
                        raise CommitSnapshotError("git archive contained a non-file .memory path")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(member) as source:
                        if source is None:
                            raise CommitSnapshotError("git archive contained an unreadable file")
                        destination.write_bytes(source.read())
        except (tarfile.TarError, OSError) as exc:
            raise CommitSnapshotError(f"could not extract commit {resolved_sha}: {exc}") from exc
        if not (root / ".memory").is_dir():
            raise CommitSnapshotError(f"commit {resolved_sha} has no .memory directory")
        yield root


def classify(rel_path: Path) -> str | None:
    if rel_path.name in TYPE_BY_FILE:
        return TYPE_BY_FILE[rel_path.name]
    parts = rel_path.parts
    if len(parts) >= 2 and parts[0] in TYPE_BY_DIR and rel_path.suffix == ".md":
        return TYPE_BY_DIR[parts[0]]
    return None


def title_for(meta: dict[str, Any], body: str, rel: Path) -> str:
    if meta.get("title"):
        ident = meta.get("id")
        return f"{ident}: {meta['title']}" if ident else str(meta["title"])
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return rel.stem


def ingest_tree(
    conn: Connection,
    repo_root: Path,
    *,
    tenant_id: UUID,
    project_id: UUID,
    principal_id: UUID | None = None,
    memory_dir: str = ".memory",
    provenance_repo: Path | None = None,
    source_ref: str | None = None,
) -> IngestReport:
    """Walk `.memory/` and reconcile it into the database."""
    report = IngestReport()
    root = repo_root / memory_dir
    if not root.is_dir():
        log.warning("no %s directory under %s", memory_dir, repo_root)
        return report

    seen_keys: set[str] = set()

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        rel_str = f"{memory_dir}/{rel.as_posix()}"

        mtype = classify(rel)
        if mtype is None:
            report.skipped.append(rel_str)
            continue
        if rel.name.upper() == "README.MD":
            # Index/navigation, not knowledge. Ingesting it produces a memory
            # that lists other memories and ranks above all of them.
            report.skipped.append(rel_str)
            continue

        raw = path.read_text(encoding="utf-8", errors="replace")

        findings = secret_scan.scan(raw)
        if findings:
            exc = secret_scan.SecretDetected(rel_str, findings)
            log.error("%s", exc)
            _record_event(conn, tenant_id, project_id, rel_str, None, "reject",
                          {"reason": "secret_detected",
                           "findings": [str(f) for f in findings]},
                          content_hash=memories.content_hash(raw))
            report.rejected.append((rel_str, str(exc)))
            continue

        meta, body = parse_frontmatter(raw)
        content = body.strip() or raw.strip()
        if len(content) > memories.MAX_CONTENT:
            msg = (f"{rel_str} is {len(content)} chars, over the {memories.MAX_CONTENT} "
                   "limit. One file is one memory and must not be chunked — split "
                   "the document into separate decisions instead.")
            log.error("%s", msg)
            _record_event(conn, tenant_id, project_id, rel_str, None, "reject",
                          {"reason": "too_large", "chars": len(content)},
                          content_hash=memories.content_hash(content))
            report.rejected.append((rel_str, msg))
            continue

        key = f"{memory_dir}:{rel.as_posix()}"
        seen_keys.add(key)
        sha = git_sha(provenance_repo or repo_root, rel_str, ref=source_ref)

        result = memories.write_memory(
            conn,
            tenant_id=tenant_id, project_id=project_id, principal_id=principal_id,
            mtype=mtype, title=title_for(meta, body, rel), content=content,
            source_type="git",            # -> authoritative (Plane A, reviewed)
            memory_key=key, source_uri=rel_str, source_version=sha,
            metadata={k: v for k, v in meta.items() if k != "title"},
        )
        _record_event(conn, tenant_id, project_id, rel_str, sha,
                      "created" if result["created"] else "unchanged",
                      {"memory_id": str(result["id"]), "tier": result["tier"]},
                      content_hash=memories.content_hash(content))
        (report.created if result["created"] else report.unchanged).append(rel_str)

    report.archived = _archive_missing(conn, tenant_id, project_id, memory_dir, seen_keys)
    return report


def _archive_missing(
    conn: Connection, tenant_id: UUID, project_id: UUID,
    memory_dir: str, seen_keys: set[str],
) -> list[str]:
    """A file that disappeared from the tree archives its memory.

    Archive rather than delete: the fact that a decision was withdrawn is part of
    the record, and the bi-temporal model (ADR-0006) has to reconstruct what the
    project believed at a past date.
    """
    rows = conn.execute(
        text("SELECT id, memory_key FROM mem.memories "
             " WHERE tenant_id = :t AND project_id = :p "
             "   AND source_type = 'git' AND status = 'active' "
             "   AND memory_key LIKE :prefix"),
        {"t": str(tenant_id), "p": str(project_id), "prefix": f"{memory_dir}:%"},
    ).mappings().all()

    gone = [r for r in rows if r["memory_key"] not in seen_keys]
    for r in gone:
        # Close valid_at as well as setting the status. Status alone leaves the
        # row occupying [lower, infinity), and memories_temporal_uniq is
        # WITHOUT OVERLAPS on (tenant_id, memory_key, valid_at) — so an archived
        # row still blocks its own key. Deleting an ADR and later restoring it
        # would fail with ExclusionViolation forever, which is a strange way to
        # discover that "archive" was only half-implemented.
        conn.execute(
            text("UPDATE mem.memories "
                 "   SET status = 'archived', superseded_at = now(), "
                 "       valid_at = tstzrange(lower(valid_at), now(), '[)') "
                 " WHERE id = :i AND upper(valid_at) IS NULL"),
            {"i": str(r["id"])},
        )
    return [r["memory_key"] for r in gone]


def _record_event(
    conn: Connection, tenant_id: UUID, project_id: UUID,
    uri: str, sha: str | None, outcome: str, payload: dict[str, Any],
    content_hash: str = "",
) -> None:
    """One row per file per run — the audit trail for what ingestion did and why.

    Rejections are recorded too. A file silently absent from the index because a
    scanner rejected it is exactly the kind of thing that gets discovered months
    later while debugging something else.
    """
    import json
    # UNIQUE (tenant_id, source_uri, content_hash) — the schema makes the event
    # log idempotent by content, which is what lets a poll loop run every minute
    # without turning this table into a heartbeat log. Re-seeing the same bytes
    # refreshes observed_at rather than appending a row; a CHANGED file has a
    # different hash and therefore does get its own event.
    conn.execute(
        text("INSERT INTO mem.ingestion_events "
             "  (tenant_id, project_id, source_type, source_uri, source_version, "
             "   content_hash, payload, occurred_at, outcome) "
             "VALUES (:t, :p, 'git', :uri, :sha, :hash, CAST(:payload AS jsonb), "
             "        now(), :outcome) "
             "ON CONFLICT (tenant_id, source_uri, content_hash) DO UPDATE "
             "   SET observed_at = now(), "
             "       outcome = EXCLUDED.outcome, "
             "       source_version = EXCLUDED.source_version"),
        {"t": str(tenant_id), "p": str(project_id), "uri": uri, "sha": sha,
         "hash": content_hash, "payload": json.dumps(payload),
         "outcome": outcome},
    )
