"""Maintenance jobs — decay, learned utility, feedback (P7d / P8, ADR-0009).

The properties under test are the ones that are easy to get backwards:

  * decay ARCHIVES, it does not delete — otherwise "what did we believe in June"
    stops working, which is the only reason the bi-temporal model exists.
  * decisions never decay. An ADR nobody asked about this month is not less true.
  * utility counts INDEPENDENT sessions. This is the defence against Suite 5's
    last attack: "an agent self-reporting its own writes as highly useful,
    repeatedly."
  * feedback moves utility and NEVER tier. A system where enough upvotes promote
    a claim to authoritative does not have a trust lattice.

    docker compose exec -T api python - < tests/test_maintenance.py
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import db, maintenance, memories  # noqa: E402

RUN = uuid.uuid4().hex[:8]
TENANT = UUID("4a1e0000-0000-0000-0000-0000000000b1")
PROJECT = UUID("4a1e0000-0000-0000-0000-0000000000b2")
PRINCIPAL = UUID("4a1e0000-0000-0000-0000-0000000000b3")
OTHER = UUID("4a1e0000-0000-0000-0000-0000000000b4")

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def seed() -> None:
    with db.engine().begin() as c:
        c.execute(text("INSERT INTO mem.organizations (id,slug,name) VALUES (:i,'maint','M') "
                       "ON CONFLICT DO NOTHING"), {"i": str(TENANT)})
        c.execute(text("INSERT INTO mem.projects (id,tenant_id,slug,name) "
                       "VALUES (:i,:t,'maint-a','M') ON CONFLICT DO NOTHING"),
                  {"i": str(PROJECT), "t": str(TENANT)})
        for pid, ext in ((PRINCIPAL, "m1"), (OTHER, "m2")):
            c.execute(text("INSERT INTO mem.principals "
                           "  (id,tenant_id,actor,external_id,display_name) "
                           "VALUES (:i,:t,'agent',:e,'m') ON CONFLICT DO NOTHING"),
                      {"i": str(pid), "t": str(TENANT), "e": f"{ext}-{pid}"})


def write(mtype, title, content, key, source="git"):
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        return memories.write_memory(
            c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype=mtype, title=title, content=content, source_type=source,
            memory_key=f"{key}-{RUN}")


def backdate(mid, days):
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        c.execute(text(
            "UPDATE mem.memories SET recorded_at = now() - make_interval(days => :d), "
            "       valid_at = tstzrange(now() - make_interval(days => :d), NULL, '[)') "
            " WHERE id = :i"), {"d": days, "i": str(mid)})


def main() -> None:
    seed()

    # ---- 1. decay archives stale episodes ---------------------------------
    print("\n1. Decay (archive, never delete)")
    old_ep = write("episode", "Bumped a dependency",
                   f"Routine dependency bump, nobody ever asked about it. {RUN}",
                   "old-episode", "commit")
    fresh_ep = write("episode", "Deployed this morning",
                     f"A recent deploy. {RUN}", "fresh-episode", "commit")
    old_dec = write("decision", "We use reciprocal rank fusion",
                    f"Arms are fused with RRF at k=60. {RUN}", "old-decision")
    backdate(old_ep["id"], 90)
    backdate(old_dec["id"], 90)

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        rep = maintenance.decay(c, tenant_id=TENANT, project_id=PROJECT)
        rows = {str(r["id"]): r for r in c.execute(text(
            "SELECT id, status::text AS st, upper(valid_at) IS NOT NULL AS closed "
            "  FROM mem.memories WHERE tenant_id = :t"),
            {"t": str(TENANT)}).mappings().all()}
    check("decay archived something", rep["archived"] >= 1, str(rep))
    check("the stale episode is archived",
          rows[str(old_ep["id"])]["st"] == "archived", rows[str(old_ep["id"])]["st"])
    check("its validity interval was closed (as_of still works)",
          rows[str(old_ep["id"])]["closed"] is True)
    check("the fresh episode is untouched",
          rows[str(fresh_ep["id"])]["st"] == "active", rows[str(fresh_ep["id"])]["st"])
    check("a 90-day-old DECISION never decays",
          rows[str(old_dec["id"])]["st"] == "active", rows[str(old_dec["id"])]["st"])

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        still_there = c.execute(text(
            "SELECT count(*) FROM mem.memories WHERE id = :i"),
            {"i": str(old_ep["id"])}).scalar_one()
        as_of_past = c.execute(text(
            "SELECT count(*) FROM mem.as_of(:p, now() - interval '60 days') "
            " WHERE id = :i"), {"p": str(PROJECT), "i": str(old_ep["id"])}).scalar_one()
    check("the row still exists (archived, not deleted)", still_there == 1)
    check("and remains answerable by as_of", as_of_past == 1, f"saw {as_of_past}")

    # ---- 2. decay is idempotent -------------------------------------------
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        again = maintenance.decay(c, tenant_id=TENANT, project_id=PROJECT)
    check("re-running decay archives nothing new", again["archived"] == 0, str(again))

    # ---- 3. utility cold-start --------------------------------------------
    print("\n2. Learned utility (ADR-0009 cold-start guard)")
    target = write("decision", "Cache invalidation strategy",
                   f"We invalidate on write, not on read. {RUN}", "utility-target")

    def fake_sessions(n, packs_prefix):
        """Simulate n retrieval events that returned `target`."""
        with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
            for i in range(n):
                c.execute(text(
                    "INSERT INTO mem.retrieval_events "
                    "  (tenant_id, project_id, principal_id, pack_id, tool, "
                    "   plan, arm_results, fused, dropped, returned_ids, "
                    "   token_count, ranking_profile, latency_ms) "
                    "VALUES (:t,:p,:pr,:pack,'memory_context','{}','{}','[]','[]', "
                    "        CAST(:ids AS uuid[]), 0, 'default@2', '{}')"),
                    {"t": str(TENANT), "p": str(PROJECT), "pr": str(PRINCIPAL),
                     "pack": f"{packs_prefix}-{i}", "ids": "{" + str(target["id"]) + "}"})

    fake_sessions(3, f"few-{RUN}")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        maintenance.recompute_utility(c, tenant_id=TENANT, project_id=PROJECT)
        u = c.execute(text("SELECT utility, retrieval_count FROM mem.memories "
                           " WHERE id = :i"), {"i": str(target["id"])}).mappings().one()
    check("below the session threshold, utility stays 0",
          float(u["utility"]) == 0.0, str(u["utility"]))
    check("but retrievals are counted", u["retrieval_count"] == 3, str(u["retrieval_count"]))

    fake_sessions(9, f"many-{RUN}")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        maintenance.recompute_utility(c, tenant_id=TENANT, project_id=PROJECT)
        u2 = c.execute(text("SELECT utility, retrieval_count FROM mem.memories "
                            " WHERE id = :i"), {"i": str(target["id"])}).mappings().one()
    check("above the threshold, utility becomes positive",
          float(u2["utility"]) > 0.0, str(u2["utility"]))
    check("sessions counted as DISTINCT packs", u2["retrieval_count"] == 12,
          str(u2["retrieval_count"]))

    # ---- 4. one session cannot manufacture utility ------------------------
    print("\n3. Suite 5 defence: repeated self-reporting does not move utility")
    loud = write("observation", "My own note",
                 f"An agent's own note it keeps citing. {RUN}", "self-cited", "agent")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        # 40 retrievals, ONE pack id — the same session, over and over.
        for _ in range(40):
            c.execute(text(
                "INSERT INTO mem.retrieval_events "
                "  (tenant_id, project_id, principal_id, pack_id, tool, plan, "
                "   arm_results, fused, dropped, returned_ids, token_count, "
                "   ranking_profile, latency_ms) "
                "VALUES (:t,:p,:pr,:pack,'memory_context','{}','{}','[]','[]', "
                "        CAST(:ids AS uuid[]), 0, 'default@2', '{}')"),
                {"t": str(TENANT), "p": str(PROJECT), "pr": str(PRINCIPAL),
                 "pack": f"one-session-{RUN}", "ids": "{" + str(loud["id"]) + "}"})
        maintenance.recompute_utility(c, tenant_id=TENANT, project_id=PROJECT)
        u3 = c.execute(text("SELECT utility, retrieval_count FROM mem.memories "
                            " WHERE id = :i"), {"i": str(loud["id"])}).mappings().one()
    check("40 retrievals in ONE session count as one",
          u3["retrieval_count"] == 1, str(u3["retrieval_count"]))
    check("utility stays 0 (no independent evidence)",
          float(u3["utility"]) == 0.0, str(u3["utility"]))

    # ---- 5. feedback moves utility, never tier ----------------------------
    print("\n4. Feedback is advisory")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        before = c.execute(text("SELECT tier::text, utility FROM mem.memories "
                                " WHERE id = :i"),
                           {"i": str(target["id"])}).mappings().one()
        for pid in (PRINCIPAL, OTHER):
            c.execute(text(
                "INSERT INTO mem.feedback (tenant_id, memory_id, principal_id, "
                "  signal, weight) VALUES (:t,:m,:p,'useful',1.0)"),
                {"t": str(TENANT), "m": str(target["id"]), "p": str(pid)})
        maintenance.recompute_utility(c, tenant_id=TENANT, project_id=PROJECT)
        after = c.execute(text("SELECT tier::text, utility FROM mem.memories "
                               " WHERE id = :i"),
                          {"i": str(target["id"])}).mappings().one()
    check("positive feedback raises utility",
          float(after["utility"]) > float(before["utility"]),
          f"{before['utility']} -> {after['utility']}")
    check("feedback NEVER changes the trust tier",
          after["tier"] == before["tier"], f"{before['tier']} -> {after['tier']}")

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        c.execute(text(
            "INSERT INTO mem.feedback (tenant_id, memory_id, principal_id, "
            "  signal, weight) VALUES (:t,:m,:p,'wrong',1.0)"),
            {"t": str(TENANT), "m": str(target["id"]), "p": str(OTHER)})
        maintenance.recompute_utility(c, tenant_id=TENANT, project_id=PROJECT)
        worse = c.execute(text("SELECT utility FROM mem.memories WHERE id = :i"),
                          {"i": str(target["id"])}).scalar_one()
    check("a `wrong` signal lowers utility", float(worse) < float(after["utility"]),
          f"{after['utility']} -> {worse}")

    # ---- 6. run_all is fault-isolated -------------------------------------
    print("\n5. run_all keeps going when a step fails")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        out = maintenance.run_all(c, tenant_id=TENANT, project_id=PROJECT)
    # `partitions` joined the sweep with monthly partitioning of
    # retrieval_events: a write into a range with no partition is an ERROR on
    # the busiest endpoint, so the window has to be kept rolling by something
    # that runs unattended.
    check("every maintenance step reported",
          set(out) == {"conflicts", "utility", "decay", "embeddings",
                       "index_advice", "partitions"},
          str(sorted(out)))
    check("the partition window is maintained", "created" in out.get("partitions", {}),
          str(out.get("partitions")))
    check("no step errored", not any("error" in v for v in out.values()), str(out)[:80])

    # ---- 6. embedding backfill (the outage path) ---------------------------
    # ADR-0008 lets a write succeed with no vector when the embedder is down.
    # Nothing used to repair that, so a transient outage caused a PERMANENT
    # hole in the vector arm. This is the repair.
    print("\n6. Embedding backfill after an outage")
    from memory_platform import embeddings as _emb
    good = _emb.provider
    _emb.provider = lambda: _emb.OllamaProvider("http://127.0.0.1:1", "bge-m3@1", 1024)
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        stranded = memories.write_memory(
            c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="decision", title=f"Stored during an outage {RUN}",
            content=f"The embedder was unreachable when this landed. {RUN}",
            source_type="git", memory_key=f"stranded-{RUN}")
    check("write survives the outage", stranded["created"] is True)
    check("but has no vector", stranded["embedded"] is False)

    _emb.provider = good
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        rep = maintenance.backfill_embeddings(c, tenant_id=TENANT, project_id=PROJECT)
        left = c.execute(text(
            "SELECT count(*) FROM mem.memories m WHERE m.tenant_id = :t "
            "  AND m.status <> 'deleted' AND upper(m.valid_at) IS NULL "
            "  AND NOT EXISTS (SELECT 1 FROM mem.memory_embeddings e "
            "                   WHERE e.memory_id = m.id)"),
            {"t": str(TENANT)}).scalar_one()
    check("backfill embeds the stranded memory", rep["embedded"] >= 1, str(rep))
    check("nothing is left unembedded", left == 0, f"{left} remaining")

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        again = maintenance.backfill_embeddings(c, tenant_id=TENANT, project_id=PROJECT)
    check("backfill is idempotent", again == {"pending": 0, "embedded": 0}, str(again))

    # It must not thrash when the embedder is still down.
    _emb.provider = lambda: _emb.OllamaProvider("http://127.0.0.1:1", "bge-m3@1", 1024)
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        memories.write_memory(
            c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="decision", title=f"Second outage write {RUN}",
            content=f"Another one during the outage. {RUN}",
            source_type="git", memory_key=f"stranded2-{RUN}")
        still_down = maintenance.backfill_embeddings(c, tenant_id=TENANT, project_id=PROJECT)
    check("backfill stops rather than retrying every row while down",
          still_down["embedded"] == 0 and still_down["pending"] >= 1, str(still_down))
    _emb.provider = good

    # ---- 7. partial index advice ------------------------------------------
    print("\n7. Partial HNSW index advice (no longer a comment nobody reads)")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        adv = maintenance.index_advice(c, tenant_id=TENANT, tenant_slug="maint")
    check("advice reports the current row count", "rows" in adv, str(adv)[:60])
    check("advice names the threshold it used", adv.get("threshold") is not None)
    check("below threshold it does not advise",
          adv["advised"] is False or adv["rows"] >= adv["threshold"], str(adv)[:60])
    check("the threshold is configuration, not a hard-coded 50000",
          maintenance.settings().partial_index_threshold != 50000,
          str(maintenance.settings().partial_index_threshold))

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*62}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
