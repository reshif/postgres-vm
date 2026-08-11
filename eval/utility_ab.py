"""Does learned utility actually improve ranking? (ADR-0009)

ADR-0009 splits two things that are easy to conflate:

    utility = evidence about usefulness
    trust   = epistemic authority

and commits to learning utility from observed retrieval and outcome feedback,
with a cold-start guard: it "only influences ranking after a memory has
accumulated enough retrievals (>= 5) to avoid" rewarding noise.

All of that is implemented. What was never done is the measurement. The active
profile carries `utility: 0.1` and `mem.ranking_profiles.eval_score` says
`{"status": "measured_by_suite_1"}` — a LABEL, not a number. Nobody had checked
whether the signal helps, hurts, or does nothing.

WHAT THIS COMPARES. The same golden set, the same corpus, the same everything,
scored twice: once under the active profile and once under a profile identical
except `utility: 0`. The difference is the signal's contribution to nDCG@10,
recall@5 and MRR.

THE CIRCULARITY, NAMED. Utility here is derived from `retrieval_events` — what
this same ranker previously retrieved. So the treatment arm amplifies the
system's own past behaviour, and the honest question is not "is utility right"
but "does amplifying what we retrieved before agree MORE with independent ground
truth, or less". The golden set is that independent ground truth, which is what
keeps the comparison meaningful. A negative result is a real finding: it would
mean the feedback loop is reinforcing its own mistakes.

WHAT IT CANNOT SAY. `mem.feedback` is empty, and utility is 0.6*usage +
0.4*feedback. So this measures the usage half with the feedback half pinned at
zero — which is also exactly the production situation, since nothing generates
feedback yet. Any claim about the feedback half is out of reach here and the
report says so rather than implying the whole signal was tested.

    docker compose exec -T api python - < eval/utility_ab.py
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
sys.path.insert(0, "/repo/eval")
from memory_platform import db, maintenance, memories, ranking  # noqa: E402

TENANT = UUID("ea1a0000-0000-0000-0000-00000000e001")
PROJECT = UUID("ea1a0000-0000-0000-0000-00000000e002")
PRINCIPAL = UUID("ea1a0000-0000-0000-0000-00000000e003")
GOLDEN = Path("/repo/eval/golden_set.json")

CONTROL_PROFILE = "ab-utility-zero"

from run_eval import mrr, ndcg_at_k, recall_at_k  # noqa: E402


def ensure_control_profile(conn) -> dict:
    """A profile identical to the active one except utility is zero.

    Inserted as a NEW profile rather than by editing weights in place, which the
    add-a-migration procedure forbids: a profile edited in place makes every
    stored retrieval_event unreproducible, because the weights that produced it
    no longer exist anywhere.
    """
    profile_id, weights = ranking.load_profile(conn)
    control = {**weights, "utility": 0.0}
    conn.execute(
        text("INSERT INTO mem.ranking_profiles (id, weights, active, eval_score) "
             "VALUES (:i, CAST(:w AS jsonb), true, "
             "        CAST(:s AS jsonb)) "
             "ON CONFLICT (id) DO UPDATE SET weights = EXCLUDED.weights"),
        {"i": CONTROL_PROFILE, "w": json.dumps(control),
         "s": json.dumps({"status": "control arm for utility A/B",
                          "derived_from": profile_id})})
    return {"treatment": profile_id, "treatment_utility": weights.get("utility"),
            "control": CONTROL_PROFILE}


def score(conn, cases: list[dict], keymap: dict[str, str],
          profile_id: str) -> dict[str, float]:
    per_case: list[dict[str, float]] = []
    for case in cases:
        hits = memories.search(conn, case["query"], limit=40, tenant_id=TENANT,
                               project_id=PROJECT, profile_id=profile_id)
        ranked = [keymap.get(str(h["id"]), "?") for h in hits]
        expected = {e["key"] for e in case["expect"]}
        grades = {e["key"]: int(e.get("grade", 3)) for e in case["expect"]}
        per_case.append({
            "recall@5": recall_at_k(ranked, expected, 5),
            "mrr": mrr(ranked, expected),
            "ndcg@10": ndcg_at_k(ranked, grades, 10),
        })
    return {k: statistics.fmean(c[k] for c in per_case) for k in per_case[0]}


def main() -> int:
    golden = json.loads(GOLDEN.read_text("utf-8"))
    cases = golden["cases"]

    # Derive utility from the retrieval evidence this tenant has actually
    # accumulated. Without this the eval tenant's utility is uniformly 0 and the
    # two arms are identical BY CONSTRUCTION — a null result that says nothing
    # about the signal.
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as conn:
        recomputed = maintenance.recompute_utility(conn, tenant_id=TENANT,
                                                   project_id=PROJECT)

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as conn:
        stats = conn.execute(text(
            "SELECT count(*) AS n, "
            "       count(*) FILTER (WHERE retrieval_count >= 5) AS over_gate, "
            "       count(DISTINCT utility) AS distinct_utility, "
            "       max(retrieval_count) AS max_retrievals "
            "  FROM mem.memories WHERE tenant_id = :t AND project_id = :p"),
            {"t": str(TENANT), "p": str(PROJECT)}).mappings().one()
        feedback = conn.execute(text(
            "SELECT count(*) FROM mem.feedback WHERE tenant_id = :t"),
            {"t": str(TENANT)}).scalar_one()

    print("=" * 74)
    print("Does learned utility improve ranking?   (ADR-0009)")
    print("=" * 74)
    print(f"  corpus                {stats['n']} memories, {len(cases)} golden cases")
    print(f"  utility recomputed    {recomputed}")
    print(f"  over the >=5 gate     {stats['over_gate']} of {stats['n']}")
    print(f"  distinct utility      {stats['distinct_utility']}")
    print(f"  max retrievals        {stats['max_retrievals']}")
    print(f"  feedback rows         {feedback}")

    # The two ways this measurement can be vacuous, checked before it is run.
    if stats["distinct_utility"] < 2:
        print("\nINCONCLUSIVE: utility has no variance, so both arms are the same")
        print("ranking by construction. Nothing about the signal can be inferred.")
        return 2
    if stats["over_gate"] == 0:
        print("\nINCONCLUSIVE: no memory passes the >=5 retrieval cold-start gate,")
        print("so utility is never applied and the arms are identical.")
        return 2

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as conn:
        profiles = ensure_control_profile(conn)
    print(f"  treatment profile     {profiles['treatment']} "
          f"(utility={profiles['treatment_utility']})")
    print(f"  control profile       {profiles['control']} (utility=0.0)")

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as conn:
        keymap = dict(conn.execute(text(
            "SELECT id::text, memory_key FROM mem.memories WHERE tenant_id = :t"),
            {"t": str(TENANT)}).all())

    print("\nscoring control arm (utility = 0) ...")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as conn:
        control = score(conn, cases, keymap, CONTROL_PROFILE)
    print("scoring treatment arm (utility as configured) ...")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as conn:
        treatment = score(conn, cases, keymap, profiles["treatment"])

    print("\n" + "-" * 74)
    print(f"  {'metric':12} {'control':>10} {'treatment':>11} {'delta':>10}")
    print("-" * 74)
    deltas = {}
    for metric in ("recall@5", "mrr", "ndcg@10"):
        delta = treatment[metric] - control[metric]
        deltas[metric] = delta
        print(f"  {metric:12} {control[metric]:>10.4f} {treatment[metric]:>11.4f} "
              f"{delta:>+10.4f}")

    headline = deltas["ndcg@10"]
    print("-" * 74)
    print()
    if abs(headline) < 0.001:
        print("VERDICT: utility makes NO measurable difference to nDCG@10.")
        print()
        print("It is carrying a weight of "
              f"{profiles['treatment_utility']} and earning nothing. ADR-0008 "
              "removes a retrieval arm")
        print("that contributes under 3%; the same reasoning applies to a ranking")
        print("feature. Either the signal needs feedback to mean anything — the")
        print("half that is pinned at zero here — or the weight should go.")
    elif headline > 0:
        print(f"VERDICT: utility IMPROVES nDCG@10 by {headline:+.4f}.")
        print()
        print("The signal earns its weight on the usage half alone. Note this is")
        print("the rich-get-richer direction: it amplifies what was retrieved")
        print("before and still agrees better with independent ground truth.")
    else:
        print(f"VERDICT: utility HURTS nDCG@10 by {headline:+.4f}.")
        print()
        print("This is the failure mode the circularity note warns about: the")
        print("loop is reinforcing the ranker's own past mistakes. The weight")
        print("should go to zero until feedback exists to correct it.")

    print()
    print("SCOPE OF THIS RESULT: mem.feedback is empty, and utility is")
    print("0.6*usage + 0.4*feedback. This measures the usage half only, with the")
    print("feedback half at zero — which is also the production situation today.")
    print("It says nothing about what utility would do once feedback exists.")

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as conn:
        conn.execute(text(
            "UPDATE mem.ranking_profiles SET eval_score = CAST(:s AS jsonb) "
            " WHERE id = :i"),
            {"i": profiles["treatment"], "s": json.dumps({
                "status": "utility_ab_measured",
                "utility_weight": profiles["treatment_utility"],
                "control": {k: round(v, 4) for k, v in control.items()},
                "treatment": {k: round(v, 4) for k, v in treatment.items()},
                "delta": {k: round(v, 4) for k, v in deltas.items()},
                "cases": len(cases),
                "caveat": "usage half only; mem.feedback is empty",
            })})
    print("\nRecorded on the profile's eval_score — numbers, replacing the")
    print("placeholder label that said 'measured_by_suite_1' and held none.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
