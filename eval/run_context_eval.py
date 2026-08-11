"""Evaluate what the MCP context-pack surface can actually support.

Suite 1 scores the candidate ranker.  An MCP client instead receives a context
pack after evidence selection, budget allocation, and the explicit no-evidence
decision.  This companion suite keeps those two measurements distinct and
persists its per-case evidence in the same evaluation history.

    docker compose exec -e MEMORY_RERANK_ENABLED=false -T api python - < eval/run_context_eval.py
"""
from __future__ import annotations

import json
import statistics
import sys
import time

from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, "/app/src")
sys.path.insert(0, "/repo/eval")
from memory_platform import context, db, evaluation, ranking  # noqa: E402
from memory_platform.config import settings  # noqa: E402
from cases import forbidden_labels, suite_one_coverage_issues, validate_golden  # noqa: E402
import run_eval  # noqa: E402


ROOT = Path("/repo/eval")
OOD = ROOT / "no_evidence_cases.json"
PACK_GATES = {
    "evidence_recall": 0.90,
    "no_evidence_precision": 1.0,
    "forbidden_in_pack": 0,
}
LATENCY_GATE_MS = 300.0


def _p95(values: list[float]) -> float:
    return sorted(values)[int(len(values) * 0.95) - 1] if values else 0.0


def _direct_keys(pack: dict, key_by_id: dict[str, str]) -> list[str]:
    """Return only entries declared as answer evidence, never baseline context."""
    result: list[str] = []
    for section in pack["sections"].values():
        for item in section:
            if item.get("context_role") not in {"evidence", "baseline_evidence"}:
                continue
            key = key_by_id.get(str(item.get("id") or item.get("ref")))
            if key:
                result.append(key)
    return result


def load_no_evidence_cases(path: Path = OOD) -> list[dict]:
    data = json.loads(path.read_text("utf-8"))
    cases = data.get("cases")
    if data.get("version") != 1 or not isinstance(cases, list) or not cases:
        raise ValueError("no-evidence suite must be a version-1 document with cases")
    ids: set[str] = set()
    for case in cases:
        case_id, query = case.get("id"), case.get("query")
        if not isinstance(case_id, str) or not case_id or case_id in ids:
            raise ValueError("no-evidence cases need unique non-empty ids")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"no-evidence case {case_id} needs a query")
        ids.add(case_id)
    return cases


def gate_failures(metrics: dict[str, float | int], p95_latency_ms: float) -> list[str]:
    failures = [name for name, gate in PACK_GATES.items()
                if (metrics[name] > gate if name == "forbidden_in_pack"
                    else metrics[name] < gate)]
    if p95_latency_ms >= LATENCY_GATE_MS:
        failures.append("p95_latency_ms")
    return failures


def run() -> int:
    golden = json.loads(run_eval.GOLDEN.read_text("utf-8"))
    validate_golden(golden)
    positive_cases = golden["cases"]
    no_evidence_cases = load_no_evidence_cases()
    corpus = run_eval.build_corpus()

    key_by_id: dict[str, str] = {}
    with db.scoped(run_eval.TENANT, run_eval.PRINCIPAL, run_eval.PROJECT) as conn:
        for memory_id, key in conn.execute(text(
            "SELECT id, memory_key FROM mem.memories WHERE tenant_id = :tenant"
        ), {"tenant": str(run_eval.TENANT)}).all():
            key_by_id[str(memory_id)] = key

    cases: list[dict] = []
    latencies: list[float] = []
    with db.scoped(run_eval.TENANT, run_eval.PRINCIPAL, run_eval.PROJECT) as conn:
        for case in positive_cases:
            started = time.perf_counter()
            pack = context.build_pack(
                conn, case["query"], tenant_id=run_eval.TENANT,
                project_id=run_eval.PROJECT, principal_id=run_eval.PRINCIPAL,
            )
            latencies.append((time.perf_counter() - started) * 1000)
            direct = _direct_keys(pack, key_by_id)
            expected = {item["key"] for item in case["expect"]}
            forbidden = {item["key"] for item in forbidden_labels(case)}
            recall = len(set(direct) & expected) / len(expected)
            cases.append({
                "case_id": case["id"], "query_text": case["query"],
                "status": "passed" if recall == 1 and not (set(direct) & forbidden) else "failed",
                "result": {
                    "kind": "positive", "answerability": pack["answerability"],
                    "evidence_keys": direct, "expected_keys": sorted(expected),
                    "evidence_recall": recall,
                    "forbidden_keys": sorted(set(direct) & forbidden),
                    "latency_ms": pack["latency_ms"],
                },
            })

        for case in no_evidence_cases:
            started = time.perf_counter()
            pack = context.build_pack(
                conn, case["query"], tenant_id=run_eval.TENANT,
                project_id=run_eval.PROJECT, principal_id=run_eval.PRINCIPAL,
            )
            latencies.append((time.perf_counter() - started) * 1000)
            direct = _direct_keys(pack, key_by_id)
            absent = pack["answerability"]["status"] == "no_relevant_evidence" and not direct
            cases.append({
                "case_id": case["id"], "query_text": case["query"],
                "status": "passed" if absent else "failed",
                "result": {
                    "kind": "no_evidence", "answerability": pack["answerability"],
                    "evidence_keys": direct, "latency_ms": pack["latency_ms"],
                },
            })

    positives = [case for case in cases if case["result"]["kind"] == "positive"]
    negatives = [case for case in cases if case["result"]["kind"] == "no_evidence"]
    metrics = {
        "evidence_recall": statistics.mean(case["result"]["evidence_recall"] for case in positives),
        "no_evidence_precision": statistics.mean(
            case["result"]["answerability"]["status"] == "no_relevant_evidence"
            and not case["result"]["evidence_keys"] for case in negatives),
        "forbidden_in_pack": sum(len(case["result"].get("forbidden_keys", [])) for case in positives),
        "positive_case_count": len(positives),
        "no_evidence_case_count": len(negatives),
    }
    p95_latency = _p95(latencies)
    coverage_issues = suite_one_coverage_issues(golden)
    missing = []
    for case in positive_cases:
        for item in [*case["expect"], *forbidden_labels(case)]:
            if item["key"] not in corpus:
                missing.append(f"{case['id']}:{item['key']}")
    failures = [*gate_failures(metrics, p95_latency), *(
        ["golden_coverage"] if coverage_issues else []), *(
        ["corpus_drift"] if missing else [])]
    status = "passed" if not failures else "failed"

    with db.scoped(run_eval.TENANT, run_eval.PRINCIPAL, run_eval.PROJECT) as conn:
        profile, _ = ranking.load_profile(conn)
        evaluation.record_run(
            conn, tenant_id=run_eval.TENANT, project_id=run_eval.PROJECT,
            principal_id=run_eval.PRINCIPAL, suite="context-answerability",
            status=status, corpus_snapshot=str(golden.get("snapshot") or ""),
            ranking_profile=profile,
            metrics={**metrics, "p95_latency_ms": round(p95_latency, 3)},
            configuration={"rerank_enabled": settings().rerank_enabled,
                           "embedding_model": settings().embedding_model},
            cases=cases,
        )

    print(f"\n{'=' * 66}\nContext-pack answerability ({len(cases)} cases)\n{'=' * 66}")
    print(f"  evidence recall         {metrics['evidence_recall']:.3f}  (gate 0.90)")
    print(f"  no-evidence precision   {metrics['no_evidence_precision']:.3f}  (gate 1.00)")
    print(f"  forbidden in pack       {metrics['forbidden_in_pack']}  (gate 0)")
    print(f"  p95 latency             {p95_latency:.0f} ms  (gate < {LATENCY_GATE_MS:.0f} ms)")
    failures_by_kind = {
        "positive": [case for case in positives if case["status"] == "failed"],
        "no-evidence": [case for case in negatives if case["status"] == "failed"],
    }
    for kind, failed in failures_by_kind.items():
        if failed:
            print(f"  {kind} failures: {len(failed)}")
            for case in failed[:5]:
                print(f"    {case['case_id']}: {case['query_text'][:72]}")
    if missing:
        print(f"  corpus drift: {len(missing)} labels missing")
    if failures:
        print(f"\nGATES FAILED: {', '.join(failures)}")
        return 1
    print("\nall gates passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
