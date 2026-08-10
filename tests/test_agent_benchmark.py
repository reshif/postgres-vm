"""Suite 7 is non-vacuous: it needs every task, arm, and repetition."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def load_benchmark():
    spec = importlib.util.spec_from_file_location("benchmark", "/repo/eval/benchmark.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def observation(task: dict, arm: str, repetition: int) -> dict:
    baseline = arm == "B"
    full = arm == "D"
    return {
        "task_id": task["id"], "arm": arm, "repetition": repetition,
        "task_success": full or (not baseline) or repetition % 2 == 0,
        "turns_to_completion": 10 if baseline else (7 if full else 9),
        "total_tokens": 100 if baseline else (105 if full else 110),
        "memory_tokens": 0 if baseline else 30,
        "wall_clock_ms": 1000, "repeated_questions": 10 if baseline else (2 if full else 8),
        "repeated_failed_approaches": 4 if baseline else (1 if full else 3),
        "incorrect_actions": 1 if baseline else 0,
        "evidence_keys": task["expected_evidence"],
        "capability_metrics": {
            "rubric_accuracy": 0.5 if baseline else 1.0,
            "provenance_correct": 1.0,
            "temporal_correct": 1.0,
            "trust_attribution_correct": 1.0,
            "leakage": 0.0,
            "procedure_steps_followed": 0.5 if baseline else 0.8,
            "preconditions_checked": 0.5 if baseline else 0.8,
        },
    }


def main() -> None:
    benchmark = load_benchmark()
    manifest = json.loads(Path("/repo/eval/agent_benchmark.json").read_text("utf-8"))
    benchmark.validate_manifest(manifest)
    assert len(manifest["tasks"]) == 20

    rows = [observation(task, arm, repetition) for task in manifest["tasks"]
            for arm in manifest["arms"] for repetition in range(1, 6)]
    result = benchmark.report(manifest, rows)
    assert result["coverage"]["complete"]
    assert result["status"] == "passed"
    assert result["headline_gate"]["passed_count"] >= 3
    assert {card["capability"] for card in result["capability_scorecard"]
            if card["status"] == "passed"} == set(benchmark.CAPABILITIES)

    incomplete = benchmark.report(manifest, rows[:-1])
    assert incomplete["status"] == "incomplete"
    assert len(incomplete["coverage"]["missing"]) == 1
    print("4/4 passed")


if __name__ == "__main__":
    main()
