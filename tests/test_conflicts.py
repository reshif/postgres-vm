"""Suite 4 — conflict handling.

00-MASTER-BLUEPRINT.md §440: "Unresolved conflicts are surfaced *in the context
pack itself* as `⚠ contested`, with both sides and dates. An agent told 'we
contest this — check with a human' behaves far better than one confidently given
the loser."

The pack has had a `contested` section since it was first written, and nothing
ever populated it — so every pack quietly asserted that the project agreed with
itself. These tests exist so that cannot silently become true again.

    docker compose exec -T api python - < tests/test_conflicts.py
"""
from __future__ import annotations

import sys
import uuid
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import conflicts, context, db, memories  # noqa: E402

RUN = uuid.uuid4().hex[:8]
TENANT = UUID("c04f0000-0000-0000-0000-0000000000d1")
PROJECT = UUID("c04f0000-0000-0000-0000-0000000000d2")
PRINCIPAL = UUID("c04f0000-0000-0000-0000-0000000000d3")

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def seed() -> None:
    with db.engine().begin() as c:
        c.execute(text("INSERT INTO mem.organizations (id,slug,name) VALUES (:i,'conf','C') "
                       "ON CONFLICT DO NOTHING"), {"i": str(TENANT)})
        c.execute(text("INSERT INTO mem.projects (id,tenant_id,slug,name) "
                       "VALUES (:i,:t,'conf-a','C') ON CONFLICT DO NOTHING"),
                  {"i": str(PROJECT), "t": str(TENANT)})
        c.execute(text("INSERT INTO mem.principals (id,tenant_id,actor,external_id,display_name) "
                       "VALUES (:i,:t,'agent',:e,'conf') ON CONFLICT DO NOTHING"),
                  {"i": str(PRINCIPAL), "t": str(TENANT), "e": f"conf-{PRINCIPAL}"})


def write(title, content, key, meta=None, mtype="decision"):
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        return memories.write_memory(
            c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype=mtype, title=title, content=content, source_type="git",
            memory_key=f"{key}-{RUN}", metadata=meta or {})


def main() -> None:
    seed()

    # ---- 1. negation detection --------------------------------------------
    print("\n1. Negation pairs (an explicit contradiction)")
    # A subject is now required: a negation pair only counts when both sides are
    # talking about the SAME thing, in proximity to it.
    check("opposed decisions about the same subject",
          conflicts.negation_pair("We will use Redis for the cache.",
                                  "We will not use Redis for the cache.",
                                  ["Redis"]) is not None)
    check("'enable' vs 'disable' about the same subject",
          conflicts.negation_pair("We enable PgBouncer pooling in production.",
                                  "We disable PgBouncer pooling in production.",
                                  ["PgBouncer"]) is not None)
    check("generic always/never is NOT a conflict",
          conflicts.negation_pair("Always run migrations first with Docker.",
                                  "Never skip review when using Docker.",
                                  ["Docker"]) is None)
    check("agreeing statements are not a conflict",
          conflicts.negation_pair("We will use Redis for the cache.",
                                  "Redis backs the cache.", ["Redis"]) is None)
    check("no shared subject means no conflict",
          conflicts.negation_pair("We will use Redis.",
                                  "We will not use Kafka.", ["Redis"]) is None)
    check("unrelated statements are not a conflict",
          conflicts.negation_pair("the sky is blue", "grass is green", []) is None)

    # ---- 2. detection over real memories ----------------------------------
    print("\n2. Detection over stored memories")
    a = write("Session cache uses Redis",
              "We will use Redis for the session cache. It is enabled in all "
              f"environments. PgBouncer is unaffected. Run {RUN}.", "redis-yes")
    b = write("Session cache must not use Redis",
              "We will not use Redis for the session cache; the store is "
              f"PostgreSQL. Redis is disabled everywhere. Run {RUN}.", "redis-no")
    unrelated = write("Grafana dashboards are provisioned from files",
                      f"Grafana reads provisioning from ops/grafana. Run {RUN}.",
                      "grafana")

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        rep = conflicts.detect(c, tenant_id=TENANT, project_id=PROJECT)
    check("detection examined the corpus", rep["examined"] >= 3, str(rep))
    check("recorded at least one candidate", rep["recorded"] >= 1, str(rep))

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        open_ = conflicts.unresolved(c, tenant_id=TENANT, project_id=PROJECT)
    ids = {s["ref"] for k in open_ for s in k["sides"]}
    check("the contradicting pair is flagged",
          str(a["id"]) in ids and str(b["id"]) in ids, f"{len(open_)} conflicts")
    # Assert the precise thing: the unrelated memory is not paired WITH the
    # contradicting pair. A blanket "appears in no conflict at all" is wrong,
    # because rows accumulate across runs and two near-identical Grafana notes
    # from different runs are a genuine near-duplicate-divergence candidate.
    paired_with_unrelated = {
        s_["ref"] for k in open_ for s_ in k["sides"]
        if str(unrelated["id"]) in {x["ref"] for x in k["sides"]}
    }
    check("the unrelated memory is not paired with the contradicting pair",
          not ({str(a["id"]), str(b["id"])} & paired_with_unrelated),
          str(paired_with_unrelated)[:60])

    # ---- 3. both sides, with dates ----------------------------------------
    print("\n3. Both sides are shown, with dates (§440)")
    if open_:
        k = open_[0]
        check("two sides recorded", len(k["sides"]) == 2, str(len(k["sides"])))
        check("each side carries a digest", all(s["digest"] for s in k["sides"]))
        check("each side carries its trust tier", all(s["trust"] for s in k["sides"]))
        check("each side carries a date", all(s["recorded_at"] for s in k["sides"]))
        check("the note tells the agent what to do",
              "human" in k["note"].lower(), k["note"][:48])

    # ---- 4. idempotency ----------------------------------------------------
    print("\n4. Re-running detection does not duplicate")
    # Measure the DELTA across a second detect, not the absolute row count.
    # Conflict rows accumulate across runs in a shared tenant, so comparing the
    # total against one run's `recorded` compares two different things and fails
    # for a reason that has nothing to do with idempotency.
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        before = c.execute(text("SELECT count(*) FROM mem.conflicts WHERE tenant_id = :t"),
                           {"t": str(TENANT)}).scalar_one()
        again = conflicts.detect(c, tenant_id=TENANT, project_id=PROJECT)
        after = c.execute(text("SELECT count(*) FROM mem.conflicts WHERE tenant_id = :t"),
                          {"t": str(TENANT)}).scalar_one()
    check("second run records nothing new", again["recorded"] == 0, str(again))
    check("conflict rows did not multiply", after == before, f"{before} -> {after}")

    # ---- 5. the pack surfaces them ----------------------------------------
    print("\n5. The pack's `contested` section is populated")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        pack = context.build_pack(c, "should the session cache use Redis?",
                                  tenant_id=TENANT, project_id=PROJECT,
                                  principal_id=PRINCIPAL, token_budget=4000)
    contested = pack["sections"]["contested"]
    check("contested section is not empty", len(contested) >= 1, f"{len(contested)}")
    check("contested entries carry both sides",
          all(len(x.get("sides", [])) == 2 for x in contested))
    check("contested is emitted last (deterministic order)",
          context.SECTION_ORDER[-1] == "contested")

    # ---- 6. never dropped for budget --------------------------------------
    # §5.4 rule 3: "Never drop conflicts to fit. Drop the lowest-utility episodes
    # instead." A tiny budget is the test that matters.
    print("\n6. Contested survives budget pressure")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        tiny = context.build_pack(c, "should the session cache use Redis?",
                                  tenant_id=TENANT, project_id=PROJECT,
                                  principal_id=PRINCIPAL,
                                  token_budget=400, window_fill_pct=48)
    check("budget really was squeezed",
          tiny["budget"]["effective"] <= context.MIN_BUDGET,
          str(tiny["budget"]["effective"]))
    check("contested is STILL present at minimum budget",
          len(tiny["sections"]["contested"]) >= 1,
          f"{len(tiny['sections']['contested'])}")

    # ---- 7. resolution clears it ------------------------------------------
    print("\n7. Resolving a conflict removes it from packs")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        c.execute(text("UPDATE mem.conflicts SET resolution = 'kept Postgres', "
                       "       resolved_at = now() WHERE tenant_id = :t"),
                  {"t": str(TENANT)})
        after = conflicts.unresolved(c, tenant_id=TENANT, project_id=PROJECT)
    check("resolved conflicts are no longer surfaced", after == [], f"{len(after)}")

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        pack2 = context.build_pack(c, "should the session cache use Redis?",
                                   tenant_id=TENANT, project_id=PROJECT,
                                   principal_id=PRINCIPAL)
    check("the pack stops warning once resolved",
          len(pack2["sections"]["contested"]) == 0,
          str(len(pack2["sections"]["contested"])))

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*62}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
