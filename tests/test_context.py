"""Context engine tests — reranking, budget allocation, pack shape, event logging.

Covers the rules from 00-MASTER-BLUEPRINT.md §5.4 that are easy to write code
for and easy to get subtly wrong: fill-percentage budgeting, digest-first
output, deterministic ordering, never dropping contested items, and storing the
score decomposition so the Retrieval Debugger can explain a past ranking.

    docker compose exec -T api python - < tests/test_context.py
"""
from __future__ import annotations

import sys
import uuid
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import context, db, memories, ranking  # noqa: E402

RUN = uuid.uuid4().hex[:8]
TENANT = UUID("77777777-0000-0000-0000-000000000077")
PROJECT = UUID("77777777-0000-0000-0000-000000000071")
PRINCIPAL = UUID("77777777-0000-0000-0000-000000000072")

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


FIXTURES = [
    ("decision", "git", "Use Postgres and pgvector as the single store",
     "We chose Postgres with pgvector over Qdrant and Weaviate so the MVP operates "
     "one datastore instead of two. Revisit above ten million vectors."),
    ("decision", "git", "Reciprocal rank fusion for hybrid retrieval",
     "Arms are fused with RRF at k=60 because it needs no score calibration "
     "between the vector, lexical and trigram arms."),
    ("procedure", "git", "How to deploy the api service",
     "Run alembic upgrade head, then docker compose up -d api, then verify "
     "readyz returns ready true before shifting traffic."),
    ("constraint", "git", "API rate limit is 100 rps per tenant",
     "The gateway enforces one hundred requests per second per tenant. Bursts "
     "above that receive HTTP 429."),
    ("failure", "ci", "Embedder OOM killed the container during warmup",
     "Text Embeddings Inference allocated the maximum batch shape at warmup and "
     "was SIGKILLed on a small host. Exit code 137 with OOMKilled false."),
    ("episode", "commit", "Bumped pgbouncer to a pinned tag",
     "Replaced the floating latest tag on pgbouncer with a pinned version."),
]


def seed() -> None:
    with db.engine().begin() as c:
        c.execute(text("INSERT INTO mem.organizations (id,slug,name) VALUES (:i,'ctx','Ctx') "
                       "ON CONFLICT DO NOTHING"), {"i": str(TENANT)})
        c.execute(text("INSERT INTO mem.projects (id,tenant_id,slug,name) "
                       "VALUES (:i,:t,'ctx-a','Ctx A') ON CONFLICT DO NOTHING"),
                  {"i": str(PROJECT), "t": str(TENANT)})
        c.execute(text("INSERT INTO mem.principals (id,tenant_id,actor,external_id,display_name) "
                       "VALUES (:i,:t,'agent',:e,'ctx') ON CONFLICT DO NOTHING"),
                  {"i": str(PRINCIPAL), "t": str(TENANT), "e": f"ctx-{PRINCIPAL}"})

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        for mtype, src, title, body in FIXTURES:
            memories.write_memory(
                c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
                mtype=mtype, title=title, content=f"{body} Run {RUN}.",
                source_type=src, memory_key=f"ctx-{RUN}-{title[:20]}",
                source_uri=(".memory/decisions/%s.md" % title[:18].replace(" ", "-")
                            if src == "git" else None),
                source_version="c0ffee1234567890" if src == "git" else None)


def main() -> None:
    seed()

    # ---- 1. pure units -----------------------------------------------------
    print("\n1. Budget allocator")
    b, note = context.effective_budget(6000, None)
    check("no fill given -> budget honoured", b == 6000, f"{b}")
    b, note = context.effective_budget(6000, 0)
    check("empty window -> full budget", b == 6000, f"{b}")
    b, note = context.effective_budget(6000, 25)
    check("half-consumed headroom halves the budget", b == 3000, f"{b}")
    b, note = context.effective_budget(6000, 50)
    check("at the 50% target -> floor, not zero", b == context.MIN_BUDGET, f"{b}")
    b, note = context.effective_budget(6000, 90)
    check("over-full window still returns a usable floor", b == context.MIN_BUDGET, f"{b}")
    check("the shrink explains itself", "scaled to" in note["reason"], note["reason"][:60])

    print("\n2. Recency decay is per type")
    from datetime import datetime, timedelta, timezone
    old = datetime.now(timezone.utc) - timedelta(days=200)
    with db.engine().connect() as c:
        _, weights = ranking.load_profile(c)
    d = ranking.recency_score("decision", old, weights)
    e = ranking.recency_score("episode", old, weights)
    check("a 200-day-old decision stays fresh", d > 0.7, f"{d:.3f}")
    check("a 200-day-old episode has decayed away", e < 0.01, f"{e:.5f}")
    check("decisions outlive episodes", d > e)

    print("\n3. Utility cold-start guard (ADR-0009)")
    cands = [
        {"id": "a", "rrf_score": 1.0, "tier": "observed", "type": "decision",
         "utility": 0.9, "retrieval_count": 2, "recorded_at": None, "identifiers": ""},
        {"id": "b", "rrf_score": 1.0, "tier": "observed", "type": "decision",
         "utility": 0.9, "retrieval_count": 9, "recorded_at": None, "identifiers": ""},
    ]
    out = {r["id"]: r for r in ranking.rerank(cands, weights=weights)}
    check("utility ignored below the retrieval threshold",
          out["a"]["score_inputs"]["utility_applied"] is False)
    check("utility applied above it",
          out["b"]["score_inputs"]["utility_applied"] is True)
    check("a well-used memory outranks an identical unused one",
          out["b"]["score"] > out["a"]["score"])

    print("\n3b. MMR diversity and deduplication")
    mmr_ranked = [
        {"id": "a", "score": 1.0},
        {"id": "b", "score": 0.95},
        {"id": "c", "score": 0.8},
    ]
    mmr_vectors = {
        "a": [1.0, 0.0],
        "b": [0.999, 0.001],
        "c": [0.0, 1.0],
    }
    mmr_kept, mmr_dropped = ranking.mmr_dedup(
        mmr_ranked,
        weights={"mmr_lambda": 0.7, "dedup_cosine": 0.94},
        vectors=mmr_vectors,
    )
    check("MMR selects the diverse candidate before a near-duplicate",
          [item["id"] for item in mmr_kept] == ["a", "c"],
          str([item["id"] for item in mmr_kept]))
    check("MMR keeps the duplicate explanation",
          len(mmr_dropped) == 1 and mmr_dropped[0]["id"] == "b"
          and mmr_kept[0].get("also_seen_in") == ["b"], str(mmr_dropped))

    # ---- 4. the pack --------------------------------------------------------
    print("\n4. Context pack")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        pack = context.build_pack(
            c, "why did we choose postgres for vectors and how do I deploy the api",
            tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            token_budget=4000)

    check("pack has an id", pack["pack_id"].startswith("pk_"), pack["pack_id"][:14])
    check("sections are in deterministic order",
          list(pack["sections"]) == context.SECTION_ORDER)
    # Tracks the code rather than pinning a version string: profiles are
    # versioned (0007 -> 0009), and a hard-coded id turns every legitimate
    # profile bump into a test failure that says nothing.
    check("it names the ranking profile used",
          pack["ranking_profile"] == ranking.DEFAULT_PROFILE, pack["ranking_profile"])
    check("not degraded (embedder up)", pack["degraded"] is False)
    check("carries the no-instructions boundary note",
          "no instructions" in pack["note"].lower())

    items = [i for s in context.SECTION_ORDER for i in pack["sections"][s]]
    check("pack returned items", len(items) >= 3, f"{len(items)} items")
    check("every item is digest-first (no full content)",
          all("content" not in i for i in items))
    check("every item carries a ref for expansion",
          all(i["ref"] for i in items))
    check("every item carries its trust tier",
          all(i["trust"] for i in items))
    check("every item carries its score decomposition",
          all(i["score_parts"] for i in items))
    check("git-sourced items cite file@commit",
          any((i["src"] or "").startswith("git:") for i in items),
          str([i["src"] for i in items if i["src"]][:1]))

    for sec, want_type in (("decisions", "decision"), ("procedures", "procedure"),
                           ("constraints", "constraint")):
        got = {i["type"] for i in pack["sections"][sec]}
        check(f"{sec} contains only {want_type}-ish types", got <= set(
            t for t, s in context.SECTION_BY_TYPE.items() if s == sec) or not got,
            str(got))

    print("\n5. Timing breakdown")
    check("stage timings recorded", set(pack["timings_ms"]) >= {"embed", "search", "rerank", "total"},
          str(pack["timings_ms"]))

    # 05-BUILD-PLAN Phase 3 acceptance is p95 < 300 ms end to end. Split the
    # assertion, because the two halves fail for completely different reasons and
    # only one of them is this codebase's to fix:
    #
    #   * The retrieval stack (ANN scan, rerank, MMR, assembly) is ours. It is
    #     asserted strictly.
    #   * The embedding call is the provider's. On CPU bge-m3 it alone costs
    #     ~350 ms, so end-to-end p95 < 300 ms is arithmetically impossible here
    #     regardless of how fast retrieval gets.
    #
    # Relaxing the end-to-end number to make the suite green would quietly delete
    # a real acceptance criterion, so it is reported instead of asserted, and the
    # requirement stands against the hardware it was written for.
    t = pack["timings_ms"]
    stack_ms = t["total"] - t.get("embed", 0)
    reranked = bool((pack.get("rerank") or {}).get("applied"))
    if reranked:
        # The cross-encoder is a second model on the hot path and blows this
        # budget BY DESIGN on CPU (~5s). Asserting the same number with it on
        # would either fail every run or force the threshold up to a value that
        # no longer means anything with it off. Reported, not asserted — the
        # trade is recorded in eval/RESULTS.md.
        print(f"  NOTE  cross-encoder enabled: stack {stack_ms}ms (budget applies "
              f"to the default configuration, where rerank is off)")
    else:
        check("retrieval stack (excl. embedding) well inside the 300ms budget",
              stack_ms < 300, f"{stack_ms}ms of a 300ms budget")
    if t["total"] >= 300:
        print(f"  NOTE  end-to-end {t['total']}ms exceeds the 300ms p95 target — "
              f"{t.get('embed', 0)}ms of it is the CPU embedder, not retrieval. "
              "Meeting the target needs a GPU/hosted embedder or a cached query vector.")

    # ---- 6. the event log ----------------------------------------------------
    print("\n6. retrieval_events (the Retrieval Debugger's source)")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        ev = c.execute(text(
            "SELECT tool, query_text, plan, arm_results, fused, dropped, returned_ids, "
            "       token_count, ranking_profile, latency_ms "
            "  FROM mem.retrieval_events WHERE pack_id = :p"),
            {"p": pack["pack_id"]}).mappings().one()
    check("one event per pack", ev["tool"] == "memory_context", ev["tool"])
    check("query text stored", ev["query_text"].startswith("why did we choose"))
    check("per-arm contribution recorded", "vector" in ev["arm_results"],
          str(ev["arm_results"]))
    check("fused list carries per-item score parts",
          bool(ev["fused"]) and "parts" in ev["fused"][0], str(ev["fused"][:1])[:70])
    check("returned ids stored", len(ev["returned_ids"]) == len(items),
          f"{len(ev['returned_ids'])} vs {len(items)}")
    check("profile recorded for reproducibility",
          ev["ranking_profile"] == ranking.DEFAULT_PROFILE, ev["ranking_profile"])
    check("query plan recorded on the event", "intent" in (ev["plan"] or {}),
          str(ev["plan"])[:60])
    check("latency stored as a stage breakdown", "total" in ev["latency_ms"],
          str(ev["latency_ms"]))

    # ---- 7. budget pressure --------------------------------------------------
    print("\n7. Budget pressure shrinks the pack")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        tight = context.build_pack(
            c, "why did we choose postgres for vectors and how do I deploy the api",
            tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            token_budget=4000, window_fill_pct=45)
    tight_items = [i for s in context.SECTION_ORDER for i in tight["sections"][s]]
    check("a nearly-full window yields a smaller budget",
          tight["budget"]["effective"] < pack["budget"]["effective"],
          f"{tight['budget']['effective']} < {pack['budget']['effective']}")
    check("and no more items than the roomy pack", len(tight_items) <= len(items),
          f"{len(tight_items)} <= {len(items)}")
    check("dropped items explain themselves",
          all(d["reason"] for d in tight["dropped"]) if tight["dropped"] else True,
          f"{len(tight['dropped'])} dropped")

    # ---- 8. quarantine ------------------------------------------------------
    print("\n8. Quarantined memories stay out unless asked for")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        memories.write_memory(
            c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="observation", title="LLM guessed the deploy steps",
            content=f"An agent inferred the API deploy procedure from a README. Run {RUN}.",
            source_type="agent", memory_key=f"ctx-inferred-{RUN}")
        plain = context.build_pack(c, "how do I deploy the api", tenant_id=TENANT,
                                   project_id=PROJECT, principal_id=PRINCIPAL)
        with_unv = context.build_pack(c, "how do I deploy the api", tenant_id=TENANT,
                                      project_id=PROJECT, principal_id=PRINCIPAL,
                                      include_unverified=True)
    p_items = [i for s in context.SECTION_ORDER for i in plain["sections"][s]]
    u_items = [i for s in context.SECTION_ORDER for i in with_unv["sections"][s]]
    check("quarantined item absent by default",
          not any("guessed" in (i["title"] or "") for i in p_items))
    check("present when include_unverified=true",
          any("guessed" in (i["title"] or "") for i in u_items),
          f"{len(u_items)} items")
    check("and flagged as unverified",
          all(i["unverified"] for i in u_items if "guessed" in (i["title"] or "")))

    # ---- 9. the always-included set (blueprint 5.2) ----------------------
    # "project constraints, conventions and the project state digest... must
    # never lose a ranking fight." They were losing every one: a single
    # constraint document is larger than the 12% section cap, so the first item
    # never fit and `constraints` was empty on every pack ever built. Found by
    # the Retrieval Debugger, which reported "budget exhausted (0/480 tokens)"
    # — used=0 and still dropping.
    print("\n9. Always-included: constraints and conventions")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        memories.write_memory(
            c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="convention", title=f"Conventions {RUN}",
            # Deliberately larger than the 12% constraints cap (480 tokens at a
            # 4000 budget). The first version of this fixture was 412 tokens and
            # fitted, so it proved nothing about the case that was actually
            # broken.
            content=("Always open transactions with db.scoped. " * 180)[:7800] + RUN,
            source_type="git", memory_key=f"ctx-conv-{RUN}")
        big = context.build_pack(c, "something entirely unrelated to conventions",
                                 tenant_id=TENANT, project_id=PROJECT,
                                 principal_id=PRINCIPAL, token_budget=4000)
        small = context.build_pack(c, "something entirely unrelated to conventions",
                                   tenant_id=TENANT, project_id=PROJECT,
                                   principal_id=PRINCIPAL,
                                   token_budget=400, window_fill_pct=48)

    def types_in(pack, sec):
        return {i["type"] for i in pack["sections"][sec]}

    check("constraints section is populated even for an unrelated query",
          len(big["sections"]["constraints"]) >= 1,
          str(len(big["sections"]["constraints"])))
    check("it holds constraint/convention types",
          types_in(big, "constraints") <= {"constraint", "convention", "preference"},
          str(types_in(big, "constraints")))
    check("a document larger than the section cap is still admitted",
          any(int(i.get("token_cost") or 0) > int(4000 * context.ALLOCATION["constraints"])
              for i in big["sections"]["constraints"]),
          str([i.get("token_cost") for i in big["sections"]["constraints"]]))
    check("still present at minimum budget (never loses a ranking fight)",
          len(small["sections"]["constraints"]) >= 1,
          str(len(small["sections"]["constraints"])))
    check("always-included items are marked as such",
          all("always" in (i.get("score_parts") or {})
              for i in big["sections"]["constraints"]),
          str([list((i.get("score_parts") or {})) for i in big["sections"]["constraints"]][:2]))
    ids = [i["ref"] for s in context.SECTION_ORDER for i in big["sections"][s]
           if s != "contested"]
    check("no item is duplicated between always-included and ranked",
          len(ids) == len(set(ids)), f"{len(ids)} vs {len(set(ids))}")

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*62}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
