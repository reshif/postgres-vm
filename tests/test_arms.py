"""Per-arm contribution — the ADR-0008 keep-or-cut measurement.

The rule this supports licenses DELETING a retrieval arm, so the failure modes
that matter are the ones that would produce a confident wrong recommendation:

  * measuring candidate counts instead of returned items. An arm can produce
    twenty-five candidates per query and be responsible for nothing that survives
    fusion, so counting candidates says "keep" about an arm that earns nothing.
  * counting participation instead of unique contribution. The vector arm
    surfaces nearly everything; an arm that only ever agrees with it looks
    productive by participation and would be kept when cutting it costs nothing.
  * reporting a verdict from three queries, or from a window where almost no
    event carries attribution. Both look like a measurement and are not one.

    docker compose exec -T api python - < tests/test_arms.py
"""
from __future__ import annotations

import json
import sys
import uuid
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import arms, db  # noqa: E402
from memory_platform.config import settings  # noqa: E402

RUN = uuid.uuid4().hex[:8]
TENANT = UUID("a4b50000-0000-0000-0000-0000000000a1")
PRINCIPAL = UUID("a4b50000-0000-0000-0000-0000000000a3")
PROJECT = uuid.uuid5(uuid.NAMESPACE_URL, f"memory-platform:test-arms:{RUN}")

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((bool(ok), name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def seed() -> None:
    with db.engine().begin() as c:
        c.execute(text("INSERT INTO mem.organizations (id,slug,name) "
                       "VALUES (:i,'armtest','Arms') ON CONFLICT DO NOTHING"),
                  {"i": str(TENANT)})
        c.execute(text("INSERT INTO mem.projects (id,tenant_id,slug,name) "
                       "VALUES (:i,:t,:s,'Arms') ON CONFLICT DO NOTHING"),
                  {"i": str(PROJECT), "t": str(TENANT), "s": f"arms-{RUN}"})
        c.execute(text("INSERT INTO mem.principals "
                       "  (id,tenant_id,actor,external_id,display_name) "
                       "VALUES (:i,:t,'agent',:e,'arms') ON CONFLICT DO NOTHING"),
                  {"i": str(PRINCIPAL), "t": str(TENANT), "e": f"arms-{PRINCIPAL}"})


def event(conn, items: list[list[str]]) -> None:
    """Write one retrieval event whose returned items carry the given arms.

    `items` is one list of arm names per returned item.
    """
    ids = [str(uuid.uuid4()) for _ in items]
    fused = [{"id": mid, "score": 0.5, "parts": {}, "inputs": {}, "arms": a}
             for mid, a in zip(ids, items)]
    conn.execute(
        text("INSERT INTO mem.retrieval_events "
             "  (tenant_id, project_id, principal_id, pack_id, tool, query_text, "
             "   plan, arm_results, fused, dropped, returned_ids, token_count, "
             "   ranking_profile, latency_ms) "
             "VALUES (:t, :p, :pr, :pack, 'memory_context', 'q', "
             "        '{}'::jsonb, '{}'::jsonb, CAST(:fused AS jsonb), '[]'::jsonb, "
             "        CAST(:ids AS uuid[]), 10, 'test', '{}'::jsonb)"),
        {"t": str(TENANT), "p": str(PROJECT), "pr": str(PRINCIPAL),
         "pack": f"pack-{uuid.uuid4().hex[:12]}", "fused": json.dumps(fused),
         "ids": "{" + ",".join(ids) + "}"})


def main() -> None:
    seed()
    print("retrieval arm contribution\n" + "=" * 62)

    floor = float(settings().arm_contribution_floor)
    check("the floor is ADR-0008's 3%", abs(floor - 0.03) < 1e-9, str(floor))
    check("the floor is configuration, not a constant",
          hasattr(settings(), "arm_contribution_floor"))
    check("the window is configuration",
          hasattr(settings(), "arm_contribution_window_days"))

    # ------------------------------------------------ nothing measured yet
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        empty = arms.contribution(c, tenant_id=TENANT, project_id=PROJECT)
    check("an unmeasured project reports insufficient evidence",
          empty["sufficient_evidence"] is False, str(empty["events"]))
    check("and withholds the verdict rather than recommending deletion",
          all(v["verdict"] == "insufficient_evidence"
              for v in empty["arms"].values()))

    # ------------------------------------------------------------ the data
    # 100 events x 4 returned items. The vector arm surfaces everything. The
    # lexical arm only ever agrees with vector — high participation, zero unique.
    # The graph arm uniquely surfaces one item in every tenth event: ~2.5% of
    # returned items, just under the floor. Identifier never fires at all.
    minimum = int(settings().arm_contribution_min_events)
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        for i in range(minimum + 20):
            items = [["vector"], ["vector", "lexical"], ["vector", "temporal"]]
            items.append(["graph"] if i % 10 == 0 else ["vector", "lexical"])
            event(c, items)

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        report = arms.contribution(c, tenant_id=TENANT, project_id=PROJECT)
    print(f"  events={report['events']} items={report['returned_items']} "
          f"coverage={report['attribution_coverage']}")
    for arm, stats in report["arms"].items():
        print(f"    {arm:11} share={stats['share']:.3f} "
              f"unique={stats['unique_share']:.3f} -> {stats['verdict']}")

    check("evidence is now sufficient", report["sufficient_evidence"] is True)
    check("attribution coverage is reported",
          report["attribution_coverage"] > 0.9,
          str(report["attribution_coverage"]))

    vector = report["arms"]["vector"]
    lexical = report["arms"]["lexical"]
    graph = report["arms"]["graph"]
    identifier = report["arms"]["identifier"]

    check("the vector arm is kept", vector["verdict"] == "keep")
    check("participation and unique contribution are reported separately",
          lexical["share"] > lexical["unique_share"],
          f"{lexical['share']} vs {lexical['unique_share']}")
    check("an arm that only ever agrees has zero unique contribution",
          lexical["unique"] == 0, str(lexical["unique"]))
    check("...and is therefore cut, despite high participation",
          lexical["verdict"] == "cut" and lexical["share"] > 0.2,
          f"share={lexical['share']}")
    check("an arm just under the 3% floor is cut",
          graph["unique_share"] < floor and graph["verdict"] == "cut",
          str(graph["unique_share"]))
    check("an arm that never fires reads zero",
          identifier["share"] == 0.0 and identifier["unique_share"] == 0.0)
    check("contribution is measured over returned items, not candidates",
          report["returned_items"] == (minimum + 20) * 4,
          str(report["returned_items"]))

    # An arm comfortably over the floor must be kept, so the floor is a real
    # boundary rather than a value everything falls under.
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        for _ in range(40):
            event(c, [["identifier"], ["identifier"], ["vector"], ["vector"]])
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        after = arms.contribution(c, tenant_id=TENANT, project_id=PROJECT)
    check("an arm over the floor is kept",
          after["arms"]["identifier"]["unique_share"] >= floor
          and after["arms"]["identifier"]["verdict"] == "keep",
          str(after["arms"]["identifier"]["unique_share"]))

    # Events without per-item attribution must not be silently averaged in.
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        for _ in range(50):
            c.execute(
                text("INSERT INTO mem.retrieval_events "
                     "  (tenant_id, project_id, principal_id, pack_id, tool, "
                     "   plan, arm_results, fused, dropped, returned_ids, "
                     "   token_count, ranking_profile, latency_ms) "
                     "VALUES (:t, :p, :pr, :pack, 'memory_context', '{}'::jsonb, "
                     "        '{}'::jsonb, '[]'::jsonb, '[]'::jsonb, "
                     "        '{}'::uuid[], 10, 'test', '{}'::jsonb)"),
                {"t": str(TENANT), "p": str(PROJECT), "pr": str(PRINCIPAL),
                 "pack": f"old-{uuid.uuid4().hex[:12]}"})
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        mixed = arms.contribution(c, tenant_id=TENANT, project_id=PROJECT)
    check("unattributed events lower reported coverage rather than the shares",
          mixed["attribution_coverage"] < after["attribution_coverage"]
          and mixed["arms"]["vector"]["share"] == after["arms"]["vector"]["share"],
          f"coverage {after['attribution_coverage']} -> "
          f"{mixed['attribution_coverage']}")

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*62}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
