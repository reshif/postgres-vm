"""Suite 3 — temporal correctness.

04-EVALUATION.md §66, case by case:
  * "What database do we use?" after a PG15->PG17 migration -> PG17 only
  * Same query with as_of before the migration -> PG15
  * "When did we switch?" -> the migration episode, with both dates
  * A superseded decision retrieved for a "why" query -> returned as context,
    clearly marked superseded, never as current guidance
  * A memory whose valid_at ended yesterday -> excluded from current retrieval

The bi-temporal model (ADR-0006) is the thing being checked: `valid_at` is when a
claim was TRUE, `recorded_at` is when we LEARNED it. mem.as_of() filters on both,
because "what did we believe in June" is a different question from "what was
actually true in June", and a system that answers the second when asked the first
will confidently attribute today's knowledge to a past decision.

    docker compose exec -T api python - < tests/test_temporal.py
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import db, memories  # noqa: E402

RUN = uuid.uuid4().hex[:8]
TENANT = UUID("70090000-0000-0000-0000-0000000000a1")
PROJECT = UUID("70090000-0000-0000-0000-0000000000a2")
PRINCIPAL = UUID("70090000-0000-0000-0000-0000000000a3")
KEY = f"db-choice-{RUN}"

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def seed() -> None:
    with db.engine().begin() as c:
        c.execute(text("INSERT INTO mem.organizations (id,slug,name) VALUES (:i,'temporal','T') "
                       "ON CONFLICT DO NOTHING"), {"i": str(TENANT)})
        c.execute(text("INSERT INTO mem.projects (id,tenant_id,slug,name) "
                       "VALUES (:i,:t,'tmp-a','T') ON CONFLICT DO NOTHING"),
                  {"i": str(PROJECT), "t": str(TENANT)})
        c.execute(text("INSERT INTO mem.principals (id,tenant_id,actor,external_id,display_name) "
                       "VALUES (:i,:t,'agent',:e,'tmp') ON CONFLICT DO NOTHING"),
                  {"i": str(PRINCIPAL), "t": str(TENANT), "e": f"tmp-{PRINCIPAL}"})


def main() -> None:
    seed()
    now = datetime.now(timezone.utc)
    long_ago = now - timedelta(days=120)
    switch = now - timedelta(days=30)

    # ---- set up the PG15 -> PG17 history ----------------------------------
    print("\n1. Building a superseded history")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        old = memories.write_memory(
            c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="decision", title="We run PostgreSQL 15",
            content=("The production database is PostgreSQL 15. Chosen for the "
                     f"partition pruning improvements. Run {RUN}."),
            source_type="git", memory_key=KEY)
        # Backdate so the "before the migration" query has something to find.
        c.execute(text(
            "UPDATE mem.memories SET recorded_at = :t, "
            "       valid_at = tstzrange(:t, NULL, '[)') WHERE id = :i"),
            {"t": long_ago, "i": str(old["id"])})

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        new = memories.write_memory(
            c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="decision", title="We run PostgreSQL 17",
            content=("The production database is PostgreSQL 17 after the upgrade. "
                     f"PostgreSQL 15 is retired. Run {RUN}."),
            source_type="git", memory_key=KEY)
        check("writing the same key supersedes the old version",
              new["superseded"] == str(old["id"]), str(new["superseded"])[:12])
        # Place the supersession boundary at the migration date.
        c.execute(text(
            "UPDATE mem.memories SET valid_at = tstzrange(lower(valid_at), :t, '[)') "
            " WHERE id = :i"), {"t": switch, "i": str(old["id"])})
        c.execute(text(
            "UPDATE mem.memories SET recorded_at = :t, "
            "       valid_at = tstzrange(:t, NULL, '[)') WHERE id = :i"),
            {"t": switch, "i": str(new["id"])})

        episode = memories.write_memory(
            c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="episode", title="Migrated the database from 15 to 17",
            content=("We upgraded PostgreSQL from 15 to 17 during the maintenance "
                     f"window. Run {RUN}."),
            source_type="commit", memory_key=f"migration-episode-{RUN}")

    # ---- 2. current retrieval sees only the current version ---------------
    print("\n2. \"What database do we use?\" -> current version only")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        hits = memories.search(c, "what database version do we run in production",
                               limit=10, tenant_id=TENANT, project_id=PROJECT)
    titles = [h["title"] for h in hits]
    check("PostgreSQL 17 is returned", any("17" in t for t in titles), str(titles[:3]))
    check("PostgreSQL 15 is NOT returned as current",
          not any("PostgreSQL 15" in t for t in titles), str(titles[:3]))

    # ---- 3. as_of before the migration ------------------------------------
    print("\n3. as_of before the migration -> the old answer")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        before = c.execute(text(
            "SELECT title FROM mem.as_of(:p, :t) WHERE memory_key = :k"),
            {"p": str(PROJECT), "t": switch - timedelta(days=1), "k": KEY}
        ).scalars().all()
        after = c.execute(text(
            "SELECT title FROM mem.as_of(:p, :t) WHERE memory_key = :k"),
            {"p": str(PROJECT), "t": now, "k": KEY}).scalars().all()
    check("as_of(before) returns PostgreSQL 15",
          any("15" in t for t in before) and not any("17" in t for t in before),
          str(before))
    check("as_of(now) returns PostgreSQL 17",
          any("17" in t for t in after) and not any("15" in t for t in after),
          str(after))

    # ---- 4. bi-temporal: belief time, not just validity -------------------
    print("\n4. Bi-temporal — what we BELIEVED then, not what we know now")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        # A fact recorded today about a period long past must NOT appear in an
        # as_of query for that period: we did not know it at the time.
        late = memories.write_memory(
            c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="failure", title="Root cause found for the old outage",
            content=("Investigation showed the 15-era outage was caused by a "
                     f"missing index. Discovered only now. Run {RUN}."),
            source_type="ci", memory_key=f"late-finding-{RUN}")
        c.execute(text(
            "UPDATE mem.memories SET valid_at = tstzrange(:v, NULL, '[)') WHERE id = :i"),
            {"v": long_ago, "i": str(late["id"])})   # was TRUE back then...
        # ...but recorded_at stays now: we only learned it today.
        seen_then = c.execute(text(
            "SELECT count(*) FROM mem.as_of(:p, :t) WHERE id = :i"),
            {"p": str(PROJECT), "t": switch, "i": str(late["id"])}).scalar_one()
        # SQL now(), not the Python `now` captured before these inserts. as_of
        # filters on `recorded_at <= p_at`, so a row recorded microseconds after
        # the timestamp under test is correctly invisible — the first version of
        # this assertion failed for exactly that reason and looked like a bug in
        # as_of rather than in the fixture.
        seen_now = c.execute(text(
            "SELECT count(*) FROM mem.as_of(:p, now()) WHERE id = :i"),
            {"p": str(PROJECT), "i": str(late["id"])}).scalar_one()
    check("a fact learned today is NOT in an as_of query for the past",
          seen_then == 0, f"saw {seen_then}")
    check("the same fact IS visible as of now", seen_now == 1, f"saw {seen_now}")

    # ---- 5. superseded is context, never current guidance -----------------
    print("\n5. A superseded decision is context, marked as such")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        row = c.execute(text(
            "SELECT status::text AS st, upper(valid_at) IS NOT NULL AS closed "
            "  FROM mem.memories WHERE id = :i"), {"i": str(old["id"])}).mappings().one()
    check("the old version is marked superseded", row["st"] == "superseded", row["st"])
    check("its validity interval is closed", row["closed"] is True)

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        sup = c.execute(text(
            "SELECT count(*) FROM mem.memory_supersessions "
            " WHERE new_id = :n AND old_id = :o"),
            {"n": str(new["id"]), "o": str(old["id"])}).scalar_one()
    check("the supersession edge is recorded for memory_explain", sup == 1, str(sup))

    # ---- 6. a validity window that ended is excluded ----------------------
    print("\n6. A memory whose valid_at ended yesterday is excluded")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        expired = memories.write_memory(
            c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="constraint", title="Temporary rate limit during the incident",
            content=f"Rate limit reduced to 10 rps for the incident window. {RUN}.",
            source_type="git", memory_key=f"temp-limit-{RUN}")
        # Backdate recorded_at as well as valid_at. A constraint that APPLIED
        # three days ago was also KNOWN three days ago; leaving recorded_at at
        # insert time models a rule that was in force before anyone wrote it
        # down, which as_of correctly refuses to return.
        c.execute(text(
            "UPDATE mem.memories SET valid_at = tstzrange(:a, :b, '[)'), "
            "       recorded_at = :a WHERE id = :i"),
            {"a": now - timedelta(days=3), "b": now - timedelta(days=1),
             "i": str(expired["id"])})
        current = c.execute(text(
            "SELECT count(*) FROM mem.as_of(:p, now()) WHERE id = :i"),
            {"p": str(PROJECT), "i": str(expired["id"])}).scalar_one()
        during = c.execute(text(
            "SELECT count(*) FROM mem.as_of(:p, :t) WHERE id = :i"),
            {"p": str(PROJECT), "t": now - timedelta(days=2),
             "i": str(expired["id"])}).scalar_one()
    check("excluded from current retrieval", current == 0, f"saw {current}")
    check("still visible as of when it applied", during == 1, f"saw {during}")

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        hits = memories.search(c, "what is the rate limit right now", limit=10,
                               tenant_id=TENANT, project_id=PROJECT)
    check("the expired constraint is not returned by search",
          not any(str(h["id"]) == str(expired["id"]) for h in hits), f"{len(hits)} hits")

    # ---- 7. "when did we switch?" -----------------------------------------
    print("\n7. \"When did we switch?\" finds the migration episode")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        hits = memories.search(c, "when did we migrate the database to 17",
                               limit=10, tenant_id=TENANT, project_id=PROJECT)
    check("the migration episode is retrievable",
          any(str(h["id"]) == str(episode["id"]) for h in hits), f"{len(hits)} hits")

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*62}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
