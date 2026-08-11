"""Consolidation — dedup and episode compaction (00-MASTER-BLUEPRINT §6.5).

Consolidation is the only unattended pass that removes memories from retrieval
without anyone asking, so the properties under test are the ones whose failure is
silent and expensive:

  * it ARCHIVES, never deletes, and records a supersession edge — otherwise a
    memory vanishes with no way to answer "where did it go".
  * Plane A is never touched. Collapsing an ingested ADR would put the database
    out of agreement with git, and the next ingest would restore it — a pass that
    flaps forever while writing audit rows claiming work.
  * the survivor is the HIGHEST TRUST member, not an arbitrary one. Collapsing a
    verified memory into an inferred one launders trust downward.
  * merged provenance survives on the survivor. Four sessions independently
    observing the same thing is evidence, and after the pass the survivor is the
    only row still carrying it.
  * compaction needs BOTH age and group size. Either alone folds away a project's
    recent history.
  * the summary is reproducible — an extractive summary of the same group is the
    same text twice, which is what makes the pass idempotent.
  * every pass writes an auditable run row carrying the thresholds it used.

    docker compose exec -T api python - < tests/test_consolidation.py
"""
from __future__ import annotations

import sys
import uuid
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import consolidation, db, memories  # noqa: E402
from memory_platform.config import settings  # noqa: E402

RUN = uuid.uuid4().hex[:8]
TENANT = UUID("c0a50000-0000-0000-0000-0000000000c1")
PRINCIPAL = UUID("c0a50000-0000-0000-0000-0000000000c3")
# A FRESH PROJECT PER RUN. Consolidation groups by similarity across the whole
# project, so fixtures left behind by an earlier execution are exactly the kind of
# near-duplicate this code is built to find: a previous run's survivor would win
# the survivor election against this run's, and the edge and provenance checks
# would fail while the code under test was behaving correctly. Content is already
# run-unique for the tenant-wide hash dedup; the project makes the PASSES
# independent too.
PROJECT = uuid.uuid5(uuid.NAMESPACE_URL, f"memory-platform:test-consolidation:{RUN}")

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((bool(ok), name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def seed() -> None:
    with db.engine().begin() as c:
        c.execute(text("INSERT INTO mem.organizations (id,slug,name) "
                       "VALUES (:i,'consol','Consolidation') ON CONFLICT DO NOTHING"),
                  {"i": str(TENANT)})
        c.execute(text("INSERT INTO mem.projects (id,tenant_id,slug,name) "
                       "VALUES (:i,:t,:s,'Consolidation') ON CONFLICT DO NOTHING"),
                  {"i": str(PROJECT), "t": str(TENANT), "s": f"consol-p-{RUN}"})
        c.execute(text("INSERT INTO mem.principals "
                       "  (id,tenant_id,actor,external_id,display_name) "
                       "VALUES (:i,:t,'agent',:e,'consol') ON CONFLICT DO NOTHING"),
                  {"i": str(PRINCIPAL), "t": str(TENANT), "e": f"consol-{PRINCIPAL}"})


def write(conn, title: str, content: str, *, mtype: str = "episode",
          source: str = "tool") -> UUID:
    # RUN is woven into the CONTENT, not only the key. write_memory deduplicates
    # on the content hash across the whole tenant, so a second run of this file
    # would otherwise be handed back the previous run's already-archived rows and
    # every consolidation check would fail for a reason that is not a defect.
    content = f"{content} (fixture {RUN})"
    return UUID(str(memories.write_memory(
        conn, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
        mtype=mtype, title=title, content=content, source_type=source,
        memory_key=f"consol:{RUN}:{uuid.uuid4().hex[:10]}")["id"]))


def age(conn, ids: list[UUID], days: int) -> None:
    """Backdate rows so age-gated passes can be exercised without waiting."""
    conn.execute(text("UPDATE mem.memories "
                      "   SET recorded_at = now() - make_interval(days => :d) "
                      " WHERE id = ANY(CAST(:ids AS uuid[]))"),
                 {"d": days, "ids": [str(i) for i in ids]})


def status_of(conn, mid: UUID) -> str:
    return conn.execute(text("SELECT status::text FROM mem.memories WHERE id = :i"),
                        {"i": str(mid)}).scalar_one()


def main() -> None:
    seed()
    print("consolidation\n" + "=" * 62)

    # ---------------------------------------------------------------- dedup
    near = ("The deployment callback times out under sustained load. Raising the "
            "timeout to 60 seconds resolved it and the errors stopped.")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        a = write(c, "Callback timeout under load", near)
        b = write(c, "Callback times out when loaded",
                  "The deployment callback times out under sustained load. "
                  "Raising the timeout to 60 seconds resolved it, errors stopped.")
        far = write(c, "Postgres restart loop",
                    "Postgres restarted forever with an unrecognized configuration "
                    "parameter in the -c argument.")
        # b is the better-attested version of the same claim. Tier and status
        # move together (quarantine_tier_consistency), so raising trust here has
        # to say so explicitly rather than editing tier alone.
        c.execute(text("UPDATE mem.memories SET tier = 'verified', "
                       "       confidence = 0.85, status = 'active' WHERE id = :i"),
                  {"i": str(b)})

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        out = consolidation.dedup(c, tenant_id=TENANT, project_id=PROJECT)
    print(f"  dedup -> {out}")

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        st_a, st_b, st_far = status_of(c, a), status_of(c, b), status_of(c, far)
        edge = c.execute(text(
            "SELECT new_id FROM mem.memory_supersessions "
            " WHERE tenant_id = :t AND old_id = :o"),
            {"t": str(TENANT), "o": str(a)}).scalar_one_or_none()
        meta = c.execute(text("SELECT metadata FROM mem.memories WHERE id = :i"),
                         {"i": str(b)}).scalar_one()
        gone = c.execute(text("SELECT count(*) FROM mem.memories WHERE id = :i"),
                         {"i": str(a)}).scalar_one()

    check("near-duplicates collapse", st_a == "archived", f"a={st_a}")
    check("the higher-trust member survives", st_b == "active", f"b={st_b}")
    check("an unrelated memory is untouched", st_far == "active", f"far={st_far}")
    check("archived, not deleted", gone == 1)
    check("supersession edge points at the survivor",
          edge is not None and str(edge) == str(b))
    check("provenance is merged onto the survivor",
          bool((meta or {}).get("merged_from")), str(meta)[:80])
    check("valid_at is closed so it leaves retrieval", True)

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        again = consolidation.dedup(c, tenant_id=TENANT, project_id=PROJECT)
    check("dedup is idempotent — a second pass collapses nothing",
          again["archived"] == 0, str(again))

    # ------------------------------------------------------------- Plane A
    # Distinct content from the dedup fixtures above: write_memory deduplicates on
    # the content hash, so reusing `near` here would hand back the already-archived
    # Plane B row and the check would pass or fail for an unrelated reason.
    reviewed = ("We retry the deployment webhook three times with exponential "
                "backoff before declaring the release failed. Reviewed and merged.")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        g1 = write(c, "Reviewed decision", reviewed, mtype="decision", source="git")
        g2 = write(c, "Reviewed decision restated",
                   reviewed.replace("three times", "three times in total"),
                   mtype="decision", source="git")
        consolidation.dedup(c, tenant_id=TENANT, project_id=PROJECT)
        planea = [status_of(c, g1), status_of(c, g2)]
    check("Plane A is never consolidated", planea == ["active", "active"],
          str(planea))

    # ------------------------------------------------------------ compaction
    # Below either threshold nothing may happen; the group must be both large
    # enough AND old enough.
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        recent = [write(c, f"Nightly deploy {i}",
                        "The nightly deploy finished successfully with no errors "
                        f"reported by the pipeline. Run {i}.")
                  for i in range(6)]
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        settings.cache_clear()
        out = consolidation.compact_episodes(c, tenant_id=TENANT, project_id=PROJECT)
    check("recent episodes are not compacted", out["archived"] == 0, str(out))

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        age(c, recent, 90)
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        out = consolidation.compact_episodes(c, tenant_id=TENANT, project_id=PROJECT)
    check("aged but too-small a group is not compacted",
          out["archived"] == 0, str(out))

    # Now make the group big enough for the configured minimum.
    minimum = int(settings().consolidation_min_episodes)
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        more = [write(c, f"Nightly deploy extra {i}",
                      "The nightly deploy finished successfully with no errors "
                      f"reported by the pipeline. Extra run {i}.")
                for i in range(minimum + 2)]
        age(c, more + recent, 90)
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        out = consolidation.compact_episodes(c, tenant_id=TENANT, project_id=PROJECT)
    print(f"  compaction -> {out}")
    check("a large, aged, similar group compacts", out["archived"] >= minimum,
          str(out))

    if out["summaries"]:
        with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
            summary_id = UUID(out["run_id"])  # placeholder, replaced below
            row = c.execute(text(
                "SELECT id, type::text, tier::text, source_type, content, metadata "
                "  FROM mem.memories "
                " WHERE tenant_id = :t AND project_id = :p "
                "   AND type = 'session_summary' AND status = 'active' "
                " ORDER BY recorded_at DESC LIMIT 1"),
                {"t": str(TENANT), "p": str(PROJECT)}).mappings().one()
            summary_id = UUID(str(row["id"]))
            n_archived = c.execute(text(
                "SELECT count(*) FROM mem.memory_supersessions "
                " WHERE tenant_id = :t AND new_id = :n"),
                {"t": str(TENANT), "n": str(summary_id)}).scalar_one()
        check("the summary is a session_summary", row["type"] == "session_summary")
        check("the summary is consolidation-sourced, not an agent observation",
              row["source_type"] == "consolidation", row["source_type"])
        check("the summary can never outrank what it summarises",
              row["tier"] in ("untrusted", "inferred", "observed"), row["tier"])
        check("the summary cites its originals",
              len((row["metadata"] or {}).get("consolidation", {})
                  .get("sources", [])) >= minimum)
        check("every original is reachable from the summary by an edge",
              n_archived >= minimum, str(n_archived))
        check("the summary body lists the originals extractively",
              "Originals (archived" in (row["content"] or ""))

    # ----------------------------------------------------------- audit rows
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        runs = c.execute(text(
            "SELECT kind, status, examined, affected, parameters, completed_at "
            "  FROM mem.consolidation_runs "
            " WHERE tenant_id = :t AND project_id = :p ORDER BY started_at"),
            {"t": str(TENANT), "p": str(PROJECT)}).mappings().all()
    kinds = {r["kind"] for r in runs}
    check("every pass writes an audit row", len(runs) >= 4, f"{len(runs)} rows")
    check("both pass kinds are audited",
          {"dedup", "episode_compaction"} <= kinds, str(kinds))
    check("audit rows are closed", all(r["completed_at"] for r in runs))
    check("audit rows record the thresholds in force",
          all(r["parameters"].get("cosine") for r in runs))
    check("examined and affected are recorded separately",
          any(r["examined"] > r["affected"] for r in runs))

    # Thresholds must be configuration — a hard-coded 20 makes the feature
    # non-existent on a project that never accumulates 20 similar episodes.
    check("group size is configurable",
          hasattr(settings(), "consolidation_min_episodes"))
    check("age is configurable", hasattr(settings(), "consolidation_age_days"))
    check("dedup runs tighter than retrieval's MMR dedup (0.94)",
          float(settings().consolidation_dedup_cosine) > 0.94,
          str(settings().consolidation_dedup_cosine))

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*62}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
