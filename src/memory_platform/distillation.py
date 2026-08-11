"""Procedure distillation — the pass that must NOT write to the database.

00-MASTER-BLUEPRINT §6.5, item 3:

    >= N (default 4) successful episodes with a consistent action sequence ->
    **open a pull request against `.memory/procedures/` in the repo.** Not a
    database write. A human reviews the procedure like code.

This is the one consolidation pass whose output is a proposal rather than a
record, and the reason is ADR-0002. A procedure is Plane A: authoritative,
reviewed, git-versioned. If distillation wrote `mem.memories` directly it would
mint authoritative-looking knowledge from unreviewed evidence, and the next
ingest would find no file behind it — the row would either be archived as missing
or persist as a phantom procedure that no reviewer ever saw and no file explains.

So the invariant is enforced structurally, not by convention: nothing in this
module imports the write path, and `distill` is given a read-only view of what it
found. The only durable trace it leaves in the database is the
`mem.consolidation_runs` audit row saying what it proposed.

WHERE THE WORK HAPPENS. On a CLONE in a temporary directory, never on the
checkout the operator is using. An unattended nightly job that creates branches
and commits inside someone's working tree is a hazard regardless of how careful
the branch naming is, and the ingestion checkout is mounted read-only in this
deployment anyway.

WHEN A PULL REQUEST CANNOT BE OPENED. There may be no remote, no credentials, or
no `gh`. The pass degrades to emitting a patch and recording the full proposed
document in the audit row — it does NOT degrade to writing the memory instead.
Falling back to a database write would trade the one property this pass exists to
have for the appearance of having done something.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .config import settings

log = logging.getLogger("memory.distillation")

GIT_TIMEOUT = 120

# Volatile fragments of a command that must not make two runs of the same action
# look like different actions: absolute paths, hashes, uuids, ports, timestamps,
# and bare numbers. Without this, "consistent action sequence" never matches
# twice and the pass silently never fires.
_VOLATILE = [
    (re.compile(r"\b[0-9a-f]{7,40}\b", re.I), "<sha>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
                re.I), "<uuid>"),
    (re.compile(r"(/[\w.-]+){2,}"), "<path>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?\b"), "<date>"),
    (re.compile(r":\d{2,5}\b"), ":<port>"),
    (re.compile(r"\b\d+\b"), "<n>"),
]


def normalise_action(command: str) -> str:
    """Reduce a command to the action it represents.

    Two runs of the same procedure differ in commit shas, container ids, ports
    and timestamps. Those differences are exactly what must be erased before
    asking whether two episodes performed the same sequence.
    """
    value = " ".join((command or "").strip().split())
    for pattern, replacement in _VOLATILE:
        value = pattern.sub(replacement, value)
    return value[:200]


def _successes(conn: Connection, *, tenant_id: UUID, project_id: UUID,
               limit: int) -> list[dict[str, Any]]:
    """Successful, deterministic-capture episodes eligible for distillation.

    Restricted to captured successes rather than any `success`-typed memory:
    ADR-0015 caps this path at deterministic capture, and a procedure distilled
    from LLM-extracted text would be an unreviewed document proposing itself as
    a reviewed one.
    """
    return [dict(r) for r in conn.execute(
        text("SELECT m.id, m.title, m.content, m.recorded_at, m.metadata, "
             "       m.tier::text AS tier, m.source_type "
             "  FROM mem.memories m "
             " WHERE m.tenant_id = :t AND m.project_id = :p "
             "   AND m.status = 'active' AND upper(m.valid_at) IS NULL "
             "   AND m.type = 'success' "
             "   AND m.metadata ? 'capture' "
             " ORDER BY m.recorded_at ASC LIMIT :limit"),
        {"t": str(tenant_id), "p": str(project_id), "limit": limit},
    ).mappings().all()]


def _action_of(row: dict[str, Any]) -> str:
    meta = row["metadata"] or {}
    command = meta.get("command") or ""
    if not command:
        # capture_tool_result puts the command in the body, not the metadata.
        match = re.search(r"`([^`]+)`", row["content"] or "")
        command = match.group(1) if match else (meta.get("tool") or row["title"])
    return normalise_action(command)


def group_episodes(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group successes into candidate procedures by their action sequence.

    Episodes carrying a `session_id` are grouped into the ordered sequence of
    actions performed in that session, and sessions sharing a sequence form the
    candidate — that is the blueprint's "consistent action sequence" exactly.

    Captures without a session are grouped by the single action they represent.
    That is a weaker signal and is deliberately kept separate rather than mixed
    in: it means "we ran this successfully N times", which is still a procedure
    worth proposing, but the distinction is visible in the proposal.
    """
    sessions: dict[str, list[dict[str, Any]]] = defaultdict(list)
    singles: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in rows:
        session = (row["metadata"] or {}).get("session_id")
        if session:
            sessions[str(session)].append(row)
        else:
            singles[_action_of(row)].append(row)

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for members in sessions.values():
        ordered = sorted(members, key=lambda m: m["recorded_at"])
        sequence = " -> ".join(_action_of(m) for m in ordered)
        # One entry per SESSION, not per episode: four tool calls in one session
        # is one piece of evidence that the sequence works, not four.
        groups[sequence].append(ordered[0] | {"_sequence_members": ordered})
    for action, members in singles.items():
        groups[action].extend(members)
    return groups


def render_procedure(sequence: str, members: list[dict[str, Any]],
                     *, project_slug: str) -> tuple[str, str]:
    """Render the proposed Plane A document. Returns (filename, markdown).

    Written as a PROPOSAL, in the frontmatter and in the body: the reviewer has
    to be able to tell at a glance that this document was machine-derived from
    evidence rather than authored, because everything else in
    `.memory/procedures/` was authored and carries a human's judgement.
    """
    steps = [s.strip() for s in sequence.split("->") if s.strip()]
    first = min(m["recorded_at"] for m in members)
    last = max(m["recorded_at"] for m in members)
    slug = re.sub(r"[^a-z0-9]+", "-",
                  (steps[0] or "procedure").lower()).strip("-")[:48] or "procedure"
    filename = f"{slug}.md"

    lines = [
        "---",
        f"id: PROC-PROPOSED-{slug}",
        f"title: {steps[0][:80]}",
        # NOT `active`. ingest.lifecycle_for treats unknown states as "no
        # opinion", so a proposal that is merged before review would become
        # retrievable; `proposed` says plainly what it is in the reviewer's diff.
        "status: proposed",
        f"date: {datetime.now(timezone.utc).date().isoformat()}",
        "source: distilled",
        "---",
        "",
        f"# {steps[0][:80]}",
        "",
        "> **Proposed by consolidation, not yet reviewed.** This document was",
        f"> distilled from {len(members)} successful runs recorded between",
        f"> {first.date().isoformat()} and {last.date().isoformat()}. Nothing here",
        "> has been confirmed by a person. Review it as you would any change to",
        f"> `.memory/procedures/` in {project_slug}: correct it, cut what is",
        "> incidental, and delete this banner when you accept it.",
        "",
        "## Steps",
        "",
    ]
    lines.extend(f"{i}. `{step}`" for i, step in enumerate(steps, 1))
    lines += ["", "## Evidence", "",
              "The successful runs this was derived from:", ""]
    for member in sorted(members, key=lambda m: m["recorded_at"])[:20]:
        lines.append(f"- `{member['id']}` — {member['recorded_at'].date().isoformat()}"
                     f" — {member['title'][:100]}")
    if len(members) > 20:
        lines.append(f"- ...and {len(members) - 20} more")
    lines += ["", "## Why this is a proposal", "",
              "Deterministic capture records that a sequence succeeded. It cannot",
              "know whether the sequence was necessary, whether a step was",
              "incidental to the machine it ran on, or whether it is the way this",
              "should be done. That judgement is the review.", ""]
    return filename, "\n".join(lines)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=GIT_TIMEOUT, check=False)


def open_pull_request(repo_root: Path, filename: str, body: str, *,
                      branch: str, output_dir: Path,
                      remote: str = "origin") -> dict[str, Any]:
    """Propose the document as a branch, and a pull request where possible.

    Operates on a CLONE. The nightly pass must not create branches or commits in
    the checkout an operator is working in, and in this deployment the ingestion
    checkout is mounted read-only regardless.

    Returns what actually happened. The caller must not treat "no pull request"
    as licence to write the memory instead — that is the whole point of the pass.
    """
    result: dict[str, Any] = {"branch": branch, "opened": False,
                              "path": f".memory/procedures/{filename}"}
    workdir = Path(tempfile.mkdtemp(prefix="memory-distill-"))
    try:
        clone = _git(["clone", "--no-hardlinks", "--quiet", str(repo_root),
                      str(workdir / "repo")], workdir)
        if clone.returncode != 0:
            result["error"] = f"clone failed: {clone.stderr.strip()[:200]}"
            return result
        repo = workdir / "repo"

        _git(["config", "user.email", "consolidation@memory-platform.local"], repo)
        _git(["config", "user.name", "memory-platform consolidation"], repo)
        checkout = _git(["checkout", "-b", branch], repo)
        if checkout.returncode != 0:
            result["error"] = f"branch failed: {checkout.stderr.strip()[:200]}"
            return result

        target = repo / ".memory" / "procedures" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        _git(["add", str(target.relative_to(repo))], repo)
        message = (f"Propose distilled procedure: {filename}\n\n"
                   "Machine-derived from successful captured runs by the "
                   "consolidation pass (00-MASTER-BLUEPRINT §6.5). Not reviewed.\n")
        commit = _git(["commit", "-m", message], repo)
        if commit.returncode != 0:
            result["error"] = f"commit failed: {commit.stderr.strip()[:200]}"
            return result
        result["commit"] = _git(["rev-parse", "HEAD"], repo).stdout.strip()[:12]

        # A patch is emitted ALWAYS, not only on failure: it is the reviewable
        # artifact, and it survives whether or not a remote accepted the branch.
        output_dir.mkdir(parents=True, exist_ok=True)
        patch = _git(["format-patch", "-1", "--stdout"], repo)
        patch_path = output_dir / f"{branch.replace('/', '-')}.patch"
        patch_path.write_text(patch.stdout, encoding="utf-8")
        result["patch"] = str(patch_path)

        origin = _git(["remote", "get-url", remote], repo_root)
        if origin.returncode != 0 or not origin.stdout.strip():
            result["status"] = "prepared_no_remote"
            result["detail"] = (f"no `{remote}` remote on {repo_root}; the patch "
                               "is the proposal")
            return result

        push = _git(["push", remote, f"{branch}:{branch}"], repo)
        if push.returncode != 0:
            result["status"] = "prepared_push_failed"
            result["detail"] = push.stderr.strip()[:300]
            return result
        result["pushed"] = True

        if shutil.which("gh") is None:
            result["status"] = "pushed_no_gh"
            result["detail"] = ("branch pushed; `gh` is not installed so the pull "
                               "request must be opened from the branch")
            return result
        pr = subprocess.run(
            ["gh", "pr", "create", "--head", branch, "--fill",
             "--title", f"Propose distilled procedure: {filename}"],
            cwd=str(repo), capture_output=True, text=True,
            timeout=GIT_TIMEOUT, check=False,
            env={**os.environ})
        if pr.returncode != 0:
            result["status"] = "pushed_pr_failed"
            result["detail"] = pr.stderr.strip()[:300]
            return result
        result["status"] = "opened"
        result["opened"] = True
        result["url"] = pr.stdout.strip().splitlines()[-1] if pr.stdout.strip() else ""
        return result
    except (OSError, subprocess.SubprocessError) as exc:
        result["error"] = str(exc)[:300]
        return result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def distill(conn: Connection, *, tenant_id: UUID, project_id: UUID,
            repo_root: Path | None = None) -> dict[str, Any]:
    """Propose procedures for action sequences that keep succeeding.

    Writes no memory. The only row it creates is the audit row.
    """
    from . import consolidation as _consolidation

    cfg = settings()
    minimum = int(cfg.distillation_min_episodes)
    limit = int(cfg.consolidation_batch_size)
    root = Path(repo_root or cfg.ingest_repo_path)
    parameters = {"min_episodes": minimum, "batch_size": limit,
                  "repo_root": str(root)}
    run_id = _consolidation._open_run(
        conn, tenant_id=tenant_id, project_id=project_id,
        kind="procedure_distillation", parameters=parameters)

    rows = _successes(conn, tenant_id=tenant_id, project_id=project_id, limit=limit)
    groups = group_episodes(rows)
    slug = conn.execute(text("SELECT slug FROM mem.projects WHERE id = :i"),
                        {"i": str(project_id)}).scalar_one_or_none() or "this project"

    # Already-proposed sequences are skipped, so a nightly pass does not open the
    # same pull request every night until someone reviews it.
    proposed = {
        str(s) for s in conn.execute(text(
            "SELECT jsonb_array_elements_text("
            "         COALESCE(details -> 'proposed_sequences', '[]'::jsonb)) "
            "  FROM mem.consolidation_runs "
            " WHERE tenant_id = :t AND project_id = :p "
            "   AND kind = 'procedure_distillation' AND id <> :self"),
            {"t": str(tenant_id), "p": str(project_id), "self": str(run_id)}).scalars()}

    proposals: list[dict[str, Any]] = []
    for sequence, members in sorted(groups.items(),
                                    key=lambda kv: -len(kv[1])):
        if len(members) < minimum or sequence in proposed:
            continue
        filename, body = render_procedure(sequence, members, project_slug=slug)
        branch = (f"{cfg.distillation_branch_prefix}"
                  f"{filename.removesuffix('.md')}-{run_id.hex[:8]}")
        outcome = open_pull_request(
            root, filename, body, branch=branch,
            output_dir=Path(cfg.distillation_output_dir))
        proposals.append({
            "sequence": sequence, "evidence": len(members),
            "filename": filename, "outcome": outcome,
            # The rendered document goes in the audit row so the proposal is
            # recoverable even when neither a remote nor the patch survives.
            "document": body[:20000],
        })

    _consolidation._close_run(
        conn, run_id, examined=len(rows), affected=len(proposals),
        details={"proposals": proposals,
                 "proposed_sequences": [p["sequence"] for p in proposals]})
    if proposals:
        log.info("distillation proposed %d procedure(s)", len(proposals))
    return {"examined": len(rows), "groups": len(groups),
            "proposals": len(proposals), "run_id": str(run_id),
            "outcomes": [p["outcome"] for p in proposals]}
