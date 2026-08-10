"""Suite 1 — retrieval accuracy against the golden set.

04-EVALUATION.md §3: `(query, expected_memory_ids, forbidden_memory_ids)` triples,
scored on recall@k (1/3/5/10), MRR, nDCG@10 with graded relevance 0-3, and
forbidden@k which must be 0. Gates: recall@5 >= 0.90, MRR >= 0.75,
nDCG@10 >= 0.70. Recall is THE gate — if the right memory is not in the top-k,
nothing downstream can save it.

The metrics are implemented here rather than pulled from a library, per §6:
"Do not take a framework dependency for arithmetic."

CASES ARE KEYED BY memory_key, NOT UUID. §5 requires cases to survive corpus
edits: UUIDs are regenerated on every re-ingest, so a UUID-keyed set would score
0.0 after a rebuild and look like a catastrophic regression. Each case also
records the content hash it was labelled against, so an ADR being *edited* is
reported as case drift rather than silently changing what the case means.

    docker compose exec -T api python - < eval/run_eval.py

Exits non-zero if a gate fails, so it works as a CI gate unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import time
import uuid
from pathlib import Path
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import context, db, evaluation, ingest, memories, ranking  # noqa: E402
from memory_platform.config import settings  # noqa: E402

# A dedicated tenant, and it must NOT be the one the scheduler's dev binding
# writes to. Those ids were the same, so poll ingestion kept adding live
# documents to the benchmark corpus between runs — the exact drift the snapshot
# exists to prevent, arriving through a different door.
TENANT = UUID("ea1a0000-0000-0000-0000-00000000e001")
PROJECT = UUID("ea1a0000-0000-0000-0000-00000000e002")
PRINCIPAL = UUID("ea1a0000-0000-0000-0000-00000000e003")

# The PINNED corpus, not the live tree. See eval/SNAPSHOT.md: a benchmark that
# moves when the product moves cannot answer "did this change help".
REPO_ROOT = Path("/repo/eval/corpus")
LIVE_ROOT = Path("/repo")          # only used to detect snapshot drift
# Resolved from the mount, not from __file__: this module is normally piped in on
# stdin (`docker compose exec -T api python - < eval/run_eval.py`), where
# __file__ does not exist and a relative path silently resolves against cwd.
GOLDEN = LIVE_ROOT / "eval" / "golden_set.json"

GATES = {"recall@5": 0.90, "mrr": 0.75, "ndcg@10": 0.70, "forbidden@10": 0.0}
LATENCY_GATE_MS = 300.0


def gate_failures(metrics: dict[str, float | int], p95_latency_ms: float) -> list[str]:
    """Return every Suite 1 acceptance failure, including end-to-end latency.

    A timeout is not a latency target. Keeping this comparison in one pure
    function makes the persisted evaluation status and the process exit code
    impossible to accidentally disagree about whether Phase 3 is accepted.
    """
    failures = [metric for metric, gate in GATES.items()
                if (metrics[metric] > gate if metric.startswith("forbidden")
                    else metrics[metric] < gate)]
    if p95_latency_ms >= LATENCY_GATE_MS:
        failures.append("p95_latency_ms")
    return failures


def corpus_paths(root: Path) -> list[str]:
    """Match snapshot.py: local CLI binding state is never benchmark content."""
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root) != Path("binding.json")
    )


# ---------------------------------------------------------------- metrics
def recall_at_k(ranked: list[str], expected: set[str], k: int) -> float:
    """Fraction of expected items present in the top k.

    Per-case recall averaged over cases (macro), not pooled: a case with five
    expected items should not outweigh four cases with one each.
    """
    if not expected:
        return 1.0
    return len(set(ranked[:k]) & expected) / len(expected)


def mrr(ranked: list[str], expected: set[str]) -> float:
    for i, r in enumerate(ranked, start=1):
        if r in expected:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: list[str], grades: dict[str, int], k: int = 10) -> float:
    """Graded relevance 0-3, standard 2^rel - 1 gain with log2 discount."""
    def dcg(items: list[str]) -> float:
        return sum((2 ** grades.get(r, 0) - 1) / math.log2(i + 1)
                   for i, r in enumerate(items[:k], start=1))
    ideal = sorted(grades, key=lambda r: grades[r], reverse=True)
    best = dcg(ideal)
    return (dcg(ranked) / best) if best > 0 else 1.0


def forbidden_at_k(ranked: list[str], forbidden: set[str], k: int) -> int:
    return len(set(ranked[:k]) & forbidden)


# ---------------------------------------------------------------- corpus
def build_corpus() -> dict[str, str]:
    """Ingest the real .memory/ tree, return {memory_key: content_hash}."""
    with db.engine().begin() as c:
        c.execute(text("INSERT INTO mem.organizations (id,slug,name) "
                       "VALUES (:i,'eval','Eval') ON CONFLICT DO NOTHING"),
                  {"i": str(TENANT)})
        c.execute(text("INSERT INTO mem.projects (id,tenant_id,slug,name) "
                       "VALUES (:i,:t,'eval-corpus','Eval') ON CONFLICT DO NOTHING"),
                  {"i": str(PROJECT), "t": str(TENANT)})
        c.execute(text("INSERT INTO mem.principals "
                       "  (id,tenant_id,actor,external_id,display_name) "
                       "VALUES (:i,:t,'agent',:e,'eval') ON CONFLICT DO NOTHING"),
                  {"i": str(PRINCIPAL), "t": str(TENANT), "e": f"eval-{PRINCIPAL}"})

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        report = ingest.ingest_tree(c, REPO_ROOT, tenant_id=TENANT,
                                    project_id=PROJECT, principal_id=PRINCIPAL)

        # Plane B: failures, successes and episodes arrive through the write path
        # with a system source_type, never as reviewed files. Seeding them is not
        # padding — without them the corpus is one type, one tier and one day, and
        # every rerank feature except RRF is constant, so the eval cannot measure
        # ranking at all. Diversity here is what makes the suite able to fail for
        # a ranking reason.
        for seed in json.loads((LIVE_ROOT / "eval" / "plane_b.json").read_text("utf-8")):
            memories.write_memory(
                c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
                mtype=seed["type"], title=seed["title"], content=seed["content"],
                source_type=seed["source"], memory_key=f"planeb:{seed['key']}")
        rows = c.execute(text(
            "SELECT memory_key, content_hash FROM mem.memories "
            " WHERE tenant_id = :t AND upper(valid_at) IS NULL"),
            {"t": str(TENANT)}).all()
    # 04-EVALUATION.md §5 requires a pinned corpus. The fingerprint makes the
    # exact benchmark input visible, so results from different snapshots cannot
    # be misread as a retrieval regression.
    fp = hashlib.sha256(
        "|".join(f"{k}:{h}" for k, h in sorted(rows)).encode()).hexdigest()[:12]
    print(f"corpus: {report.summary()}  |  {len(rows)} memories  |  fingerprint {fp}")

    # Warn when the pinned snapshot has fallen behind the live tree. Not an
    # error: the snapshot is SUPPOSED to lag, that is what makes results
    # comparable. But a snapshot nobody has refreshed for months is measuring a
    # corpus the product no longer has, and that should be visible rather than
    # discovered later.
    live = corpus_paths(LIVE_ROOT / ".memory")
    snap = corpus_paths(REPO_ROOT / ".memory")
    if live != snap:
        added, removed = set(live) - set(snap), set(snap) - set(live)
        print(f"  NOTE snapshot lags the live tree: "
              f"+{len(added)} -{len(removed)} file(s). "
              f"`python eval/snapshot.py` to re-freeze (invalidates comparisons).")
    return {k: h for k, h in rows}


def run(case_ids: set[str] | None = None) -> int:
    golden = json.loads(GOLDEN.read_text("utf-8"))
    cases = golden["cases"]
    if case_ids:
        cases = [case for case in cases if case["id"] in case_ids]
        missing_ids = case_ids - {case["id"] for case in cases}
        if missing_ids:
            print(f"unknown case id(s): {', '.join(sorted(missing_ids))}", file=sys.stderr)
            return 2
    corpus = build_corpus()

    # §5: "cases are labelled with the memory IDs AND a stable content hash, so
    # corpus edits do not silently invalidate a case."
    drift, missing = [], []
    for c in cases:
        for e in c["expect"]:
            if e["key"] not in corpus:
                missing.append((c["id"], e["key"]))
            elif e.get("hash") and e["hash"] != corpus[e["key"]][:12]:
                drift.append((c["id"], e["key"]))

    if missing:
        print(f"\n{len(missing)} case(s) reference memories not in the corpus:")
        for cid, k in missing[:8]:
            print(f"  {cid}: {k}")
    if drift:
        print(f"\n{len(drift)} case(s) labelled against different content "
              f"(the source file changed — relabel, do not just update the hash):")
        for cid, k in drift[:8]:
            print(f"  {cid}: {k}")

    key_by_id: dict[str, str] = {}
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        for mid, mkey in c.execute(text(
            "SELECT id, memory_key FROM mem.memories WHERE tenant_id = :t"),
                {"t": str(TENANT)}).all():
            key_by_id[str(mid)] = mkey

    per_case, latencies = [], []
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        for case in cases:
            t0 = time.perf_counter()
            # Scope is required for entity resolution; without it the graph
            # arm receives NULL and the eval measures a four-arm system while
            # the product runs five.
            # Score top 10, but retain a deeper diagnostic window. A zero-recall
            # case at rank 13 needs a different fix from one absent from all 40
            # candidates; without this distinction every failure looks like a
            # generic ranking problem.
            hits = memories.search(c, case["query"], limit=40,
                                   tenant_id=TENANT, project_id=PROJECT)
            latencies.append((time.perf_counter() - t0) * 1000)

            ranked = [key_by_id.get(str(h["id"]), "?") for h in hits]
            expected = {e["key"] for e in case["expect"]}
            grades = {e["key"]: int(e.get("grade", 3)) for e in case["expect"]}
            forbid = set(case.get("forbid", []))

            per_case.append({
                "id": case["id"], "query": case["query"],
                "recall@1": recall_at_k(ranked, expected, 1),
                "recall@3": recall_at_k(ranked, expected, 3),
                "recall@5": recall_at_k(ranked, expected, 5),
                "recall@10": recall_at_k(ranked, expected, 10),
                "mrr": mrr(ranked, expected),
                "ndcg@10": ndcg_at_k(ranked, grades, 10),
                "forbidden@10": forbidden_at_k(ranked, forbid, 10),
                "top": ranked[:3],
                "candidate_depth": len(ranked),
                "expected_positions": {
                    key: (ranked.index(key) + 1) if key in ranked else None
                    for key in sorted(expected)
                },
            })

    agg = {m: statistics.mean(c[m] for c in per_case)
           for m in ("recall@1", "recall@3", "recall@5", "recall@10", "mrr", "ndcg@10")}
    agg["forbidden@10"] = sum(c["forbidden@10"] for c in per_case)

    p95_latency = sorted(latencies)[int(len(latencies)*0.95)-1]
    failures = gate_failures(agg, p95_latency)
    run_status = "passed" if not failures else "failed"
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        profile, _ = ranking.load_profile(c)
        evaluation.record_run(
            c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            suite="retrieval-accuracy", status=run_status,
            corpus_snapshot=str(golden.get("snapshot") or ""),
            ranking_profile=profile,
            metrics={**agg, "p95_latency_ms": round(p95_latency, 3),
                     "forbidden@10": agg["forbidden@10"], "case_count": len(cases)},
            configuration={"rerank_enabled": settings().rerank_enabled,
                           "embedding_model": settings().embedding_model},
            cases=[{
                "case_id": case["id"], "query_text": case["query"],
                "status": "passed" if case["recall@5"] == 1
                and case["forbidden@10"] == 0 else "failed",
                "result": {key: value for key, value in case.items() if key not in {"id", "query"}},
            } for case in per_case],
        )

    print(f"\n{'='*66}\nSuite 1 — retrieval accuracy   ({len(cases)} cases)\n{'='*66}")
    for m in ("recall@1", "recall@3", "recall@5", "recall@10", "mrr", "ndcg@10"):
        gate = GATES.get(m)
        mark = "" if gate is None else ("  PASS" if agg[m] >= gate else "  FAIL")
        gate_s = f"  (gate {gate:.2f})" if gate else ""
        print(f"  {m:12} {agg[m]:.3f}{gate_s}{mark}")
    fk = agg["forbidden@10"]
    print(f"  {'forbidden@10':12} {fk:.0f}  (gate 0){'  PASS' if fk == 0 else '  FAIL'}")
    print(f"  {'p95 latency':12} {p95_latency:.0f} ms  (gate < {LATENCY_GATE_MS:.0f} ms)"
          f"  {'PASS' if p95_latency < LATENCY_GATE_MS else 'FAIL'}")

    worst = sorted(per_case, key=lambda c: (c["recall@5"], c["mrr"]))[:5]
    print(f"\nWeakest cases (these are where to look, not the average):")
    for c in worst:
        print(f"  r@5={c['recall@5']:.2f} mrr={c['mrr']:.2f}  {c['query'][:52]}")
        print(f"        top: {[t.split('/')[-1] for t in c['top']]}")
        positions = ", ".join(
            f"{key.split('/')[-1]}=" + (str(rank) if rank else f"absent@{c['candidate_depth']}")
            for key, rank in c["expected_positions"].items()
        )
        print(f"        expected: {positions}")

    if failures:
        print(f"\nGATES FAILED: {', '.join(failures)}")
        return 1
    print("\nall gates passed")
    return 0


if __name__ == "__main__":
    args = argparse.ArgumentParser(description="Run Suite 1 retrieval evaluation")
    args.add_argument("--case", action="append", dest="case_ids",
                      help="run one golden case id; repeat for multiple cases")
    parsed = args.parse_args()
    sys.exit(run(set(parsed.case_ids) if parsed.case_ids else None))
