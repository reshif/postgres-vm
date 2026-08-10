"""Plane A ingestion tests — the Phase 1 acceptance criteria.

05-BUILD-PLAN Phase 1 acceptance:
  "merging a PR that adds .memory/decisions/ADR-0007.md makes that decision
   retrievable within 60 seconds with provenance resolving to the exact commit;
   a second scope context (project B) cannot see it; re-running ingestion creates
   no duplicates; a file containing a fake AWS key is rejected with a clear
   message and an alert."

Each clause is a test below. Runs against a throwaway git repo built in a temp
directory so it does not depend on the state of this checkout.

    docker compose exec -T api python - < tests/test_ingest.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import db, ingest, memories, secret_scan, worker  # noqa: E402

RUN = uuid.uuid4().hex[:8]
TENANT = UUID("99999999-0000-0000-0000-000000000099")
PROJ_A = UUID("99999999-0000-0000-0000-0000000000a1")
PROJ_B = UUID("99999999-0000-0000-0000-0000000000b1")
PRIN = UUID("99999999-0000-0000-0000-0000000000c1")

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


ADR_TMPL = """---
id: ADR-0007
title: RLS with FORCE and leak tests as a merge gate
status: accepted
date: 2026-08-09
related:
  - ADR-0004
---

# ADR-0007: Row Level Security with FORCE

## Context

Multi-tenant isolation cannot be enforced in application code alone, because a
single missing WHERE clause silently returns another tenant's rows.

## Decision

Enable RLS with FORCE on every tenant-scoped table from the first migration, and
make the negative isolation tests a merge gate rather than a nightly job.

Run marker: {RUN}
"""

ADR = ADR_TMPL.replace("{RUN}", RUN)

CONVENTIONS = """# Conventions

Use `db.scoped()` for every transaction. There is deliberately no unscoped
equivalent for application code. Run marker: """ + RUN + """
"""

# A syntactically valid but fake AWS key. Phase 1 acceptance requires this to be
# rejected with a clear message rather than redacted.
LEAKY = """---
id: ADR-0099
title: Deploy runbook with a pasted credential
---

# Deploy

Export the key before running the deploy script:

    AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE
    aws_secret_access_key = wJalrXUtnFEMIK7MDENGbPxRfiCYzEXAMPLEKEY
"""


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args],
                         capture_output=True, text=True, check=False)
    return out.stdout.strip()


def build_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    (repo / ".memory" / "decisions").mkdir(parents=True)
    git(repo.parent, "init", str(repo))
    git(repo, "config", "user.email", "ci@example.com")
    git(repo, "config", "user.name", "CI")
    (repo / ".memory" / "decisions" / "ADR-0007.md").write_text(ADR, encoding="utf-8")
    (repo / ".memory" / "conventions.md").write_text(CONVENTIONS, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "add ADR-0007")
    return repo


def seed() -> None:
    with db.engine().begin() as c:
        c.execute(text("INSERT INTO mem.organizations (id,slug,name) VALUES (:i,'ing','Ing') "
                       "ON CONFLICT DO NOTHING"), {"i": str(TENANT)})
        for p, s in ((PROJ_A, "ing-a"), (PROJ_B, "ing-b")):
            c.execute(text("INSERT INTO mem.projects (id,tenant_id,slug,name) "
                           "VALUES (:i,:t,:s,:s) ON CONFLICT DO NOTHING"),
                      {"i": str(p), "t": str(TENANT), "s": s})
        c.execute(text("INSERT INTO mem.principals (id,tenant_id,actor,external_id,display_name) "
                       "VALUES (:i,:t,'agent',:e,'ing') ON CONFLICT DO NOTHING"),
                  {"i": str(PRIN), "t": str(TENANT), "e": f"ing-{PRIN}"})


def main() -> None:
    seed()

    # ---- 0. scanner unit behaviour ----------------------------------------
    print("\n0. Secret scanner")
    check("detects a fake AWS access key id", len(secret_scan.scan(LEAKY)) >= 1,
          str(secret_scan.scan(LEAKY)[:1]))
    check("clean ADR text produces no findings", secret_scan.scan(ADR) == [])
    check("placeholders are not flagged",
          secret_scan.scan("password=change-me\ntoken=<your-token-here>") == [])
    masked = str(secret_scan.SecretDetected("f.md", secret_scan.scan(LEAKY)))
    check("error message never echoes the full credential",
          "AKIAIOSFODNN7EXAMPLE" not in masked, masked[:56] + "…")
    check("error message says what to do", "Rotate the credential" in masked)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        repo = build_repo(tmp)
        head = git(repo, "rev-parse", "HEAD")

        # ---- 1. first ingestion ------------------------------------------
        print("\n1. First ingestion")
        with db.scoped(TENANT, PRIN, PROJ_A) as c:
            rep = ingest.ingest_tree(c, repo, tenant_id=TENANT, project_id=PROJ_A,
                                     principal_id=PRIN)
        check("ADR and conventions ingested", rep.summary()["created"] == 2, str(rep.summary()))
        check("README-style/unknown files skipped, none rejected",
              not rep.rejected, str(rep.rejected))

        # ---- 2. provenance resolves to the exact commit -------------------
        print("\n2. Provenance")
        with db.scoped(TENANT, PRIN, PROJ_A) as c:
            row = c.execute(text(
                "SELECT title, tier::text, source_uri, source_version, type::text, "
                "       metadata->>'status' AS adr_status "
                "  FROM mem.memories WHERE memory_key = :k "
                "   AND upper(valid_at) IS NULL"),
                {"k": ".memory:decisions/ADR-0007.md"}).mappings().one()
        check("source_version is the exact commit sha", row["source_version"] == head,
              (row["source_version"] or "none")[:12])
        check("source_uri points at the file in the repo",
              row["source_uri"] == ".memory/decisions/ADR-0007.md", row["source_uri"])
        check("git-sourced memory is authoritative", row["tier"] == "authoritative", row["tier"])
        check("classified as a decision", row["type"] == "decision", row["type"])
        check("frontmatter kept in metadata", row["adr_status"] == "accepted",
              str(row["adr_status"]))
        check("title combines id and frontmatter title",
              row["title"].startswith("ADR-0007:"), row["title"][:40])

        # ---- 3. retrievable ------------------------------------------------
        print("\n3. Retrievable")
        with db.scoped(TENANT, PRIN, PROJ_A) as c:
            hits = memories.search(c, "why do we force row level security", limit=5)
        check("the decision is retrievable", any("ADR-0007" in h["title"] for h in hits),
              f"{len(hits)} hits")

        # ---- 4. project B cannot see it ------------------------------------
        print("\n4. Scope isolation (Phase 1 acceptance)")
        with db.scoped(TENANT, PRIN, PROJ_B) as c:
            n = c.execute(text("SELECT count(*) FROM mem.memories "
                               "WHERE memory_key = :k AND upper(valid_at) IS NULL"),
                          {"k": ".memory:decisions/ADR-0007.md"}).scalar_one()
            check("project B cannot see project A's ADR", n == 0, f"saw {n}")
            hits_b = memories.search(c, "why do we force row level security", limit=5)
            check("project B retrieval returns none of it",
                  not any("ADR-0007" in h["title"] for h in hits_b), f"{len(hits_b)} hits")

        # ---- 5. idempotency -------------------------------------------------
        print("\n5. Re-running ingestion creates no duplicates")
        with db.scoped(TENANT, PRIN, PROJ_A) as c:
            rep2 = ingest.ingest_tree(c, repo, tenant_id=TENANT, project_id=PROJ_A,
                                      principal_id=PRIN)
        check("second run creates nothing", rep2.summary()["created"] == 0, str(rep2.summary()))
        check("second run reports them unchanged", rep2.summary()["unchanged"] == 2)
        with db.scoped(TENANT, PRIN, PROJ_A) as c:
            n = c.execute(text("SELECT count(*) FROM mem.memories WHERE memory_key = :k "
                               "AND upper(valid_at) IS NULL"),
                          {"k": ".memory:decisions/ADR-0007.md"}).scalar_one()
        check("exactly one CURRENT version of the ADR", n == 1, f"{n} rows")

        # ---- 5b. queued commit ingestion uses the named tree ----------------
        # The checkout can advance before a webhook job is claimed. The task must
        # archive the requested commit, not `git checkout` it or read whatever
        # happens to be in the working tree by then.
        print("\n5b. Queued commit ingestion is immutable")
        snapshot_text = (
            "# ADR-0008: Commit snapshots are immutable\n\n"
            f"Workers ingest the exact archive named by a webhook. Run {RUN}.\n")
        snapshot_path = repo / ".memory" / "decisions" / "ADR-0008.md"
        snapshot_path.write_text(snapshot_text, encoding="utf-8")
        git(repo, "add", "-A"); git(repo, "commit", "-m", "add snapshot ADR")
        snapshot_sha = git(repo, "rev-parse", "HEAD")
        queued = worker._ingest_git_commit(str(PROJ_A), snapshot_sha, repo_root=repo)
        check("queued worker ingests the commit snapshot", queued["created"] == 1,
              str(queued))
        check("queued worker leaves the checkout at its original HEAD",
              git(repo, "rev-parse", "HEAD") == snapshot_sha,
              git(repo, "rev-parse", "HEAD")[:12])
        with db.scoped(TENANT, PRIN, PROJ_A) as c:
            snapshot = c.execute(text(
                "SELECT content, source_version FROM mem.memories "
                " WHERE memory_key = '.memory:decisions/ADR-0008.md' "
                "   AND upper(valid_at) IS NULL")).mappings().one()
        check("queued provenance resolves to the requested commit",
              snapshot["source_version"] == snapshot_sha,
              (snapshot["source_version"] or "none")[:12])
        check("queued ingestion stored only the archived file content",
              snapshot_text.strip() in snapshot["content"], snapshot["content"][:50])

        # ---- 6. secret file is rejected -------------------------------------
        print("\n6. A file with a credential is rejected, not redacted")
        (repo / ".memory" / "decisions" / "ADR-0099.md").write_text(LEAKY, encoding="utf-8")
        git(repo, "add", "-A"); git(repo, "commit", "-m", "oops")
        with db.scoped(TENANT, PRIN, PROJ_A) as c:
            rep3 = ingest.ingest_tree(c, repo, tenant_id=TENANT, project_id=PROJ_A,
                                      principal_id=PRIN)
        check("the leaky file is rejected", len(rep3.rejected) == 1, str(rep3.summary()))
        check("rejection message names the file",
              rep3.rejected and "ADR-0099" in rep3.rejected[0][0], "")
        with db.scoped(TENANT, PRIN, PROJ_A) as c:
            n = c.execute(text("SELECT count(*) FROM mem.memories WHERE memory_key = :k"),
                          {"k": ".memory:decisions/ADR-0099.md"}).scalar_one()
            check("NOTHING from the leaky file was stored", n == 0, f"{n} rows")
            ev = c.execute(text(
                "SELECT count(*) FROM mem.ingestion_events "
                " WHERE outcome = 'reject' AND source_uri LIKE '%ADR-0099%'")).scalar_one()
            check("rejection is recorded in ingestion_events (the alert trail)",
                  ev >= 1, f"{ev} events")

        # ---- 7. deleting a file archives its memory --------------------------
        print("\n7. Deleting a file archives its memory")
        (repo / ".memory" / "decisions" / "ADR-0007.md").unlink()
        git(repo, "add", "-A"); git(repo, "commit", "-m", "retract ADR-0007")
        with db.scoped(TENANT, PRIN, PROJ_A) as c:
            rep4 = ingest.ingest_tree(c, repo, tenant_id=TENANT, project_id=PROJ_A,
                                      principal_id=PRIN)
            check("removal is reported as archived", rep4.summary()["archived"] == 1,
                  str(rep4.summary()))
            st = c.execute(text("SELECT status::text FROM mem.memories WHERE memory_key = :k "
                                "ORDER BY recorded_at DESC LIMIT 1"),
                           {"k": ".memory:decisions/ADR-0007.md"}).scalar_one()
            check("memory is ARCHIVED, not deleted", st == "archived", st)
            hits = memories.search(c, "why do we force row level security", limit=5)
            check("archived memory drops out of retrieval",
                  not any("ADR-0007" in h["title"] for h in hits), f"{len(hits)} hits")

        # ---- 8. editing a file supersedes, keeping history ------------------
        # The most common real case: an ADR gets revised. Under the bi-temporal
        # model the old version must be closed, not overwritten, and the new one
        # must open — memories_temporal_uniq (WITHOUT OVERLAPS) rejects the write
        # entirely if the prior row is left open.
        print("\n8. Editing a file supersedes the previous version")
        (repo / ".memory" / "conventions.md").write_text(
            CONVENTIONS + "\nAlways call mem.fn_set_scope inside the transaction.\n",
            encoding="utf-8")
        git(repo, "add", "-A"); git(repo, "commit", "-m", "revise conventions")
        with db.scoped(TENANT, PRIN, PROJ_A) as c:
            rep5 = ingest.ingest_tree(c, repo, tenant_id=TENANT, project_id=PROJ_A,
                                      principal_id=PRIN)
            check("edited file is re-ingested", rep5.summary()["created"] == 1,
                  str(rep5.summary()))
            versions = c.execute(text(
                "SELECT status::text AS st, upper(valid_at) IS NULL AS open "
                "  FROM mem.memories WHERE memory_key = :k ORDER BY recorded_at"),
                {"k": ".memory:conventions.md"}).mappings().all()
            check("exactly one version is still open",
                  sum(1 for v in versions if v["open"]) == 1,
                  str([(v["st"], v["open"]) for v in versions]))
            check("the previous version is marked superseded",
                  any(v["st"] == "superseded" for v in versions),
                  str([v["st"] for v in versions]))
            link = c.execute(text(
                "SELECT count(*) FROM mem.memory_supersessions s "
                "  JOIN mem.memories m ON m.id = s.new_id "
                " WHERE m.memory_key = :k"), {"k": ".memory:conventions.md"}).scalar_one()
            check("supersession edge recorded for memory.explain", link >= 1, f"{link} edges")
            cur = c.execute(text(
                "SELECT content FROM mem.memories WHERE memory_key = :k "
                "   AND upper(valid_at) IS NULL"), {"k": ".memory:conventions.md"}).scalar_one()
            check("current version has the new text", "fn_set_scope" in cur)

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*62}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
