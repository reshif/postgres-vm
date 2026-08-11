"""Suite 3 — temporal correctness. Produces the pass rate capability C3 is scored on.

04-EVALUATION.md §Suite 3 names five behaviours, and C3 wants >= 90% of them,
with arm B (grep) expected near zero — this is the capability the blueprint
argues a memory system has and a filesystem does not:

  1. "What database do we use?" after a PG15 -> PG17 migration -> PG17 only;
     PG15 excluded or explicitly marked historical
  2. the same query with `as_of` before the migration -> PG15
  3. "When did we switch?" -> the migration episode, with both dates
  4. a superseded decision retrieved for a "why" query -> returned as context,
     clearly marked superseded, never as current guidance
  5. a memory whose `valid_at` ended yesterday -> excluded from current retrieval

WHY THIS SEEDS ITS OWN SCENARIO. The other suites score the pinned `.memory`
corpus, which has no supersession history in it — every document is current, so
every temporal question has the same answer at every timestamp and the suite
would pass while measuring nothing. Temporal correctness can only be tested
against knowledge that CHANGED, so the scenario is constructed: a claim, its
replacement, and the episode that connects them, with controlled validity.

WHY IT IS SEPARATE FROM tests/test_temporal.py. That suite asserts the as-of
machinery works — the constraint, the interval, the query path. This one asks the
product question the machinery exists to serve, and reports a RATE rather than
pass/fail, because C3's threshold is 90% and a rate is what a threshold applies
to.

    docker compose exec -T api python - < eval/suite3_temporal.py
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import context, db, memories  # noqa: E402

RUN = uuid.uuid4().hex[:6]
TENANT = UUID("53000000-0000-0000-0000-0000000000a1")
PRINCIPAL = UUID("53000000-0000-0000-0000-0000000000a3")
PROJECT = uuid.uuid5(uuid.NAMESPACE_URL, f"memory-platform:suite3:{RUN}")

GATE = 0.90
NOW = datetime.now(timezone.utc)
BEFORE_MIGRATION = NOW - timedelta(days=60)
MIGRATION_DAY = NOW - timedelta(days=30)

cases: list[tuple[str, str, bool, str]] = []


def case(group: str, name: str, passed: bool, detail: str = "") -> None:
    cases.append((group, name, bool(passed), detail))
    print(f"  {'PASS' if passed else 'FAIL'}  [{group}] {name}"
          + (f"  [{detail}]" if detail else ""))


def seed_scope() -> None:
    with db.engine().begin() as c:
        c.execute(text("INSERT INTO mem.organizations (id,slug,name) "
                       "VALUES (:i,:s,'Suite 3') ON CONFLICT DO NOTHING"),
                  {"i": str(TENANT), "s": f"suite3"})
        c.execute(text("INSERT INTO mem.projects (id,tenant_id,slug,name) "
                       "VALUES (:i,:t,:s,'Suite 3') ON CONFLICT DO NOTHING"),
                  {"i": str(PROJECT), "t": str(TENANT), "s": f"suite3-{RUN}"})
        c.execute(text("INSERT INTO mem.principals "
                       "  (id,tenant_id,actor,external_id,display_name) "
                       "VALUES (:i,:t,'human',:e,'suite3') ON CONFLICT DO NOTHING"),
                  {"i": str(PRINCIPAL), "t": str(TENANT), "e": f"suite3-{PRINCIPAL}"})


def write(conn, *, key: str, title: str, content: str,
          mtype: str = "decision", source: str = "git") -> UUID:
    return UUID(str(memories.write_memory(
        conn, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
        mtype=mtype, title=title, content=content, source_type=source,
        memory_key=key)["id"]))


def backdate(conn, mid: UUID, *, start: datetime, end: datetime | None) -> None:
    """Place a memory's validity explicitly.

    write_memory opens validity at now(); a temporal suite needs claims that were
    true in the past and stopped being true at a known moment, which is not
    something the write path can express on its own.
    """
    conn.execute(
        text("UPDATE mem.memories "
             "   SET recorded_at = :start, "
             "       valid_at = tstzrange(:start, :end, '[)') "
             " WHERE id = :i"),
        {"start": start, "end": end, "i": str(mid)})


def keys_for(hits: list[dict], keymap: dict[str, str]) -> list[str]:
    return [keymap.get(str(h["id"]), "?") for h in hits]


def main() -> int:
    seed_scope()
    print("Suite 3 — temporal correctness\n" + "=" * 70)

    # ---------------------------------------------------------- the scenario
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        pg15 = write(
            c, key=f".memory:decisions/database.md",
            title="Primary database: PostgreSQL 15",
            content=("The primary database for this project is PostgreSQL 15. "
                     "All services connect to the PostgreSQL 15 cluster and "
                     "extensions are pinned to the 15 series."))
        backdate(c, pg15, start=BEFORE_MIGRATION, end=None)

        episode = write(
            c, key=f"planeb:database-migration-{RUN}",
            title="Migrated the primary database from PostgreSQL 15 to 17",
            content=(f"We switched the primary database from PostgreSQL 15 to "
                     f"PostgreSQL 17 on {MIGRATION_DAY.date().isoformat()}. The "
                     f"cluster ran PostgreSQL 15 from "
                     f"{BEFORE_MIGRATION.date().isoformat()} until that day. "
                     "Cutover took one maintenance window."),
            mtype="episode", source="tool")
        backdate(c, episode, start=MIGRATION_DAY, end=None)

        expired = write(
            c, key=f"planeb:temporary-freeze-{RUN}",
            title="Deploy freeze during the database migration",
            content=("A deploy freeze is in effect for the primary database "
                     "migration window. No schema changes may ship."),
            mtype="constraint", source="human")
        backdate(c, expired, start=NOW - timedelta(days=10),
                 end=NOW - timedelta(days=1))

    # The replacement, written LAST so supersession runs the real path: same
    # memory_key, different content, which closes the previous row's validity.
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        pg17 = write(
            c, key=f".memory:decisions/database.md",
            title="Primary database: PostgreSQL 17",
            content=("The primary database for this project is PostgreSQL 17. "
                     "All services connect to the PostgreSQL 17 cluster. This "
                     "replaced the PostgreSQL 15 cluster."))

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        keymap = dict(c.execute(text(
            "SELECT id::text, memory_key FROM mem.memories WHERE tenant_id = :t"),
            {"t": str(TENANT)}).all())
        rows = {str(r["id"]): dict(r) for r in c.execute(text(
            "SELECT id, status::text AS status, lower(valid_at) AS vfrom, "
            "       upper(valid_at) AS vuntil "
            "  FROM mem.memories WHERE tenant_id = :t"),
            {"t": str(TENANT)}).mappings().all()}

    # Confirm the scenario is real before scoring anything against it.
    case("setup", "the superseded claim is closed and marked",
         rows[str(pg15)]["status"] == "superseded"
         and rows[str(pg15)]["vuntil"] is not None,
         f"status={rows[str(pg15)]['status']}")
    case("setup", "the current claim is open",
         rows[str(pg17)]["status"] == "active"
         and rows[str(pg17)]["vuntil"] is None)
    case("setup", "the expired constraint's validity ended yesterday",
         rows[str(expired)]["vuntil"] is not None
         and rows[str(expired)]["vuntil"] < NOW)

    q_current = "what database do we use?"

    # --------------------------------------------- 1. current query -> PG17
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        hits = memories.search(c, q_current, limit=10, tenant_id=TENANT,
                               project_id=PROJECT)
        ids = [str(h["id"]) for h in hits]
    case("1 current", "the current answer is returned", str(pg17) in ids,
         f"{len(ids)} hits")
    case("1 current", "the superseded answer is NOT returned as current",
         str(pg15) not in ids)

    # ------------------------------------- 2. as_of before -> the old answer
    as_of = MIGRATION_DAY - timedelta(days=5)
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        hits = memories.search(c, q_current, limit=10, tenant_id=TENANT,
                               project_id=PROJECT, as_of=as_of)
        ids_then = [str(h["id"]) for h in hits]
    case("2 as_of", "asking about the past returns what was true then",
         str(pg15) in ids_then, f"{len(ids_then)} hits")
    case("2 as_of", "and not the answer that had not happened yet",
         str(pg17) not in ids_then)

    # ------------------------------------------- 3. "when did we switch?"
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        hits = memories.search(c, "when did we switch the database?", limit=10,
                               tenant_id=TENANT, project_id=PROJECT)
        ids_when = [str(h["id"]) for h in hits]
    case("3 when", "the migration episode is returned", str(episode) in ids_when)
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        body = c.execute(text("SELECT content FROM mem.memories WHERE id = :i"),
                         {"i": str(episode)}).scalar_one()
    case("3 when", "it carries both dates",
         BEFORE_MIGRATION.date().isoformat() in body
         and MIGRATION_DAY.date().isoformat() in body)

    # ------------------- 4. superseded decision surfaced as context, MARKED
    #
    # The query is deliberately one the project CAN answer historically. An
    # earlier version asked "why did we choose our current database version?" at
    # a past timestamp, which the answerability check correctly declined — there
    # was no evidence about choosing — so the case passed on a substring found in
    # `dropped` and then failed its own marker assertion. It was testing nothing.
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        pack = context.build_pack(
            c, "what database do we use?",
            tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            token_budget=4000, as_of=as_of)
    items = [i for section in (pack.get("sections") or {}).values()
             for i in (section if isinstance(section, list) else [])]
    shown = [i for i in items if str(i.get("id")) == str(pg15)]
    case("4 superseded", "a historical pack surfaces the decision of the day",
         bool(shown), f"{len(items)} items in pack")
    case("4 superseded", "and marks it as no longer current",
         bool(shown) and (shown[0].get("historical") is True
                          or str(shown[0].get("status")) == "superseded"),
         json.dumps({k: shown[0].get(k) for k in ("historical", "status", "trust")},
                    default=str) if shown else "not surfaced")

    # The other half of the blueprint's sentence — "never as current guidance" —
    # is the exclusion already asserted in case 1. Stated separately because the
    # two halves can fail independently: marking without excluding still hands an
    # agent stale guidance, and excluding without marking makes the as-of view
    # unreadable.
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        now_pack = context.build_pack(
            c, "what database do we use?", tenant_id=TENANT, project_id=PROJECT,
            principal_id=PRINCIPAL, token_budget=4000)
    now_items = [i for section in (now_pack.get("sections") or {}).values()
                 for i in (section if isinstance(section, list) else [])]
    case("4 superseded", "and never appears in a CURRENT pack",
         all(str(i.get("id")) != str(pg15) for i in now_items),
         f"{len(now_items)} items in current pack")

    # ----------------------------- 5. expired validity excluded from current
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        hits = memories.search(c, "is there a deploy freeze in effect?", limit=10,
                               tenant_id=TENANT, project_id=PROJECT)
        ids_now = [str(h["id"]) for h in hits]
    case("5 expired", "a memory whose validity ended yesterday is excluded",
         str(expired) not in ids_now, f"{len(ids_now)} hits")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        hits = memories.search(c, "is there a deploy freeze in effect?", limit=10,
                               tenant_id=TENANT, project_id=PROJECT,
                               as_of=NOW - timedelta(days=5))
        ids_back = [str(h["id"]) for h in hits]
    case("5 expired", "but is still answerable as of when it was in force",
         str(expired) in ids_back, f"{len(ids_back)} hits")

    # ------------------------------------------------------------- result
    passed = sum(1 for _, _, ok, _ in cases if ok)
    rate = passed / len(cases)
    print("\n" + "=" * 70)
    print(f"  Suite 3 pass rate: {passed}/{len(cases)} = {rate:.1%}   "
          f"(C3 gate >= {GATE:.0%})   {'PASS' if rate >= GATE else 'FAIL'}")
    print("=" * 70)

    failed = [(g, n, d) for g, n, ok, d in cases if not ok]
    if failed:
        print("\nfailing cases:")
        for g, n, d in failed:
            print(f"  [{g}] {n}" + (f"  — {d}" if d else ""))

    # Recorded under the DEV project, not the fixture tenant this suite seeds in.
    #
    # The scenario needs an isolated, per-run project — its memory keys collide
    # across runs by design, since supersession is the thing under test. But the
    # RESULT is a statement about the platform's capability for the project being
    # operated, and the go/no-go reads that scope. Recording it in the throwaway
    # tenant put it behind RLS where the scorecard could not see it, and C3 read
    # "never run" with a completed run sitting in the table.
    from memory_platform.config import settings as _settings
    dev_tenant = UUID(_settings().dev_tenant_id)
    dev_project = UUID(_settings().dev_project_id)
    with db.scoped(dev_tenant, dev_tenant, dev_project) as c:
        c.execute(text(
            "INSERT INTO mem.evaluation_runs "
            "  (tenant_id, project_id, suite, status, metrics, completed_at) "
            "VALUES (:t, :p, 'temporal-correctness', :s, CAST(:m AS jsonb), now())"),
            {"t": str(dev_tenant), "p": str(dev_project),
             "s": "passed" if rate >= GATE else "failed",
             "m": json.dumps({
                 "pass_rate": round(rate, 4), "passed": passed,
                 "case_count": len(cases), "gate": GATE,
                 "failing": [f"{g}: {n}" for g, n, _ in failed],
                 "fixture_tenant": str(TENANT),
             })})
    print("\nRecorded as suite 'temporal-correctness' for the C3 scorecard line.")
    return 0 if rate >= GATE else 1


if __name__ == "__main__":
    raise SystemExit(main())
