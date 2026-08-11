"""Procedure distillation — proposes a pull request, never writes a memory.

This is the Phase 7 acceptance property, and it is the only one that really
matters here: a procedure is Plane A (ADR-0002), so distillation writing
`mem.memories` would mint authoritative-looking knowledge out of unreviewed
evidence, with no file behind it for any reviewer to have seen.

The test therefore counts memories before and after and demands the number is
unchanged — including on the degraded paths, where no remote exists and no pull
request can be opened. "It could not open a PR, so it wrote the row instead"
would be the exact failure this pass is designed to make impossible.

Also under test:
  * >= N successful runs are required, N is configuration, not a constant.
  * volatile fragments (shas, ports, timestamps, paths) do not stop two runs of
    the same action from being recognised as the same action — otherwise the
    grouping never matches twice and the feature silently never fires.
  * the proposal is marked `proposed`, not `active`, so merging it before review
    does not make it retrievable.
  * the same sequence is not re-proposed on every nightly pass.

    docker compose exec -T api python - < tests/test_distillation.py
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
from memory_platform import capture, db, distillation  # noqa: E402
from memory_platform.config import settings  # noqa: E402

RUN = uuid.uuid4().hex[:8]
TENANT = UUID("d1571110-0000-0000-0000-0000000000d1")
PRINCIPAL = UUID("d1571110-0000-0000-0000-0000000000d3")
PROJECT = uuid.uuid5(uuid.NAMESPACE_URL, f"memory-platform:test-distillation:{RUN}")

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((bool(ok), name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def seed() -> None:
    with db.engine().begin() as c:
        c.execute(text("INSERT INTO mem.organizations (id,slug,name) "
                       "VALUES (:i,'distill','Distillation') ON CONFLICT DO NOTHING"),
                  {"i": str(TENANT)})
        c.execute(text("INSERT INTO mem.projects (id,tenant_id,slug,name) "
                       "VALUES (:i,:t,:s,'Distillation') ON CONFLICT DO NOTHING"),
                  {"i": str(PROJECT), "t": str(TENANT), "s": f"distill-{RUN}"})
        c.execute(text("INSERT INTO mem.principals "
                       "  (id,tenant_id,actor,external_id,display_name) "
                       "VALUES (:i,:t,'agent',:e,'distill') ON CONFLICT DO NOTHING"),
                  {"i": str(PRINCIPAL), "t": str(TENANT), "e": f"distill-{PRINCIPAL}"})


def count_memories(conn) -> int:
    return conn.execute(text(
        "SELECT count(*) FROM mem.memories WHERE tenant_id = :t AND project_id = :p"),
        {"t": str(TENANT), "p": str(PROJECT)}).scalar_one()


def make_repo() -> Path:
    """A throwaway git repo standing in for the project checkout."""
    root = Path(tempfile.mkdtemp(prefix=f"distill-repo-{RUN}-"))
    (root / ".memory" / "procedures").mkdir(parents=True)
    (root / ".memory" / "conventions.md").write_text("# Conventions\n", encoding="utf-8")
    for args in (["init", "-q", "-b", "main"],
                 ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "Test"],
                 ["add", "."], ["commit", "-q", "-m", "initial"]):
        subprocess.run(["git", *args], cwd=str(root), check=True,
                       capture_output=True, text=True)
    return root


def main() -> None:
    seed()
    print("procedure distillation\n" + "=" * 62)

    # ---------------------------------------------------- action normalisation
    a = distillation.normalise_action(
        "docker compose exec -T api pytest /app/tests/test_x.py --seed 8f3a91c2b7")
    b = distillation.normalise_action(
        "docker compose exec -T api pytest /app/tests/test_x.py --seed 41bd9e0a3c")
    check("volatile fragments do not split one action into two", a == b, a)
    check("the action itself survives normalisation", "pytest" in a, a)
    c1 = distillation.normalise_action("alembic upgrade head")
    check("different actions stay different", c1 != a)

    # ------------------------------------------------------------ the fixtures
    session_ids = [f"{RUN}-s{i}" for i in range(5)]
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        before_seed = count_memories(c)
        for i, session in enumerate(session_ids):
            # The same two-step sequence, run five times with volatile differences.
            capture.capture_tool_result(
                c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
                tool="alembic", exit_code=0, session_id=session,
                command=f"alembic upgrade head --tag deploy-{uuid.uuid4().hex[:8]}",
                output_excerpt=f"ok {i}")
            capture.capture_tool_result(
                c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
                tool="pytest", exit_code=0, session_id=session,
                command=f"pytest tests/ --seed {uuid.uuid4().hex[:8]}",
                output_excerpt=f"passed {i}")
        seeded = count_memories(c) - before_seed
    check("captures landed", seeded >= 2, f"{seeded} memories")

    repo = make_repo()

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        before = count_memories(c)
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        out = distillation.distill(c, tenant_id=TENANT, project_id=PROJECT,
                                   repo_root=repo)
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        after = count_memories(c)
    print(f"  distill -> proposals={out['proposals']} "
          f"outcomes={[o.get('status') or o.get('error') for o in out['outcomes']]}")

    # ------------------------------------------- THE acceptance property
    check("distillation writes NO memory", after == before,
          f"{before} -> {after}")
    check("it proposed something", out["proposals"] >= 1, str(out["proposals"]))

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        row = c.execute(text(
            "SELECT kind, examined, affected, details, parameters "
            "  FROM mem.consolidation_runs "
            " WHERE tenant_id = :t AND project_id = :p "
            "   AND kind = 'procedure_distillation' "
            " ORDER BY started_at DESC LIMIT 1"),
            {"t": str(TENANT), "p": str(PROJECT)}).mappings().one()
    proposals = row["details"].get("proposals", [])
    check("the run is audited", row["kind"] == "procedure_distillation")
    check("the audit row records the threshold used",
          row["parameters"].get("min_episodes") == settings().distillation_min_episodes,
          str(row["parameters"].get("min_episodes")))
    check("the proposed document is recoverable from the audit row",
          bool(proposals and proposals[0].get("document")))

    document = proposals[0]["document"] if proposals else ""
    check("the proposal is marked proposed, not active",
          "status: proposed" in document)
    check("the proposal says it was not reviewed",
          "not yet reviewed" in document.lower())
    check("the proposal cites its evidence", "## Evidence" in document)
    check("the proposal lists the action sequence as steps",
          "alembic upgrade head" in document and "pytest" in document)

    outcome = proposals[0]["outcome"] if proposals else {}
    check("a branch was created", bool(outcome.get("branch")))
    check("a commit was made on the branch", bool(outcome.get("commit")))
    check("a reviewable patch was emitted", bool(outcome.get("patch")))
    check("the target path is under .memory/procedures/",
          str(outcome.get("path", "")).startswith(".memory/procedures/"),
          str(outcome.get("path")))
    check("it reports honestly that no PR was opened without a remote",
          outcome.get("opened") is False
          and str(outcome.get("status")).startswith("prepared"),
          str(outcome.get("status")))

    # The operator's checkout must be untouched — the work happens on a clone.
    branches = subprocess.run(["git", "branch", "--list"], cwd=str(repo),
                              capture_output=True, text=True, check=False).stdout
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                           capture_output=True, text=True, check=False).stdout
    check("the source checkout gains no branch",
          settings().distillation_branch_prefix.split("/")[0] not in branches
          or branches.count("\n") <= 1, branches.strip()[:60])
    check("the source checkout is left clean", dirty.strip() == "", dirty[:60])
    check("no procedure file was written into the source checkout",
          not list((repo / ".memory" / "procedures").glob("*.md")))

    # -------------------------------------------------------- not re-proposed
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        second = distillation.distill(c, tenant_id=TENANT, project_id=PROJECT,
                                      repo_root=repo)
    check("a second pass does not re-propose the same sequence",
          second["proposals"] == 0, str(second["proposals"]))

    # ------------------------------------------------------ threshold is a knob
    check("the minimum is configuration, not a hard-coded 4",
          hasattr(settings(), "distillation_min_episodes"))
    check("the blueprint default of 4 is what ships",
          settings().distillation_min_episodes == 4,
          str(settings().distillation_min_episodes))

    # Below the threshold nothing is proposed.
    lonely = uuid.uuid5(uuid.NAMESPACE_URL, f"distill-lonely-{RUN}")
    with db.engine().begin() as c:
        c.execute(text("INSERT INTO mem.projects (id,tenant_id,slug,name) "
                       "VALUES (:i,:t,:s,'Lonely') ON CONFLICT DO NOTHING"),
                  {"i": str(lonely), "t": str(TENANT), "s": f"lonely-{RUN}"})
    with db.scoped(TENANT, PRINCIPAL, lonely) as c:
        capture.capture_tool_result(
            c, tenant_id=TENANT, project_id=lonely, principal_id=PRINCIPAL,
            tool="make", exit_code=0, command=f"make build {RUN}",
            output_excerpt="ok")
    with db.scoped(TENANT, PRINCIPAL, lonely) as c:
        thin = distillation.distill(c, tenant_id=TENANT, project_id=lonely,
                                    repo_root=repo)
    check("one success is not a procedure", thin["proposals"] == 0, str(thin))

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*62}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
