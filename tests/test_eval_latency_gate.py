"""Suite 1's latency acceptance is a real gate, not a display-only timeout."""
from __future__ import annotations

import importlib.util
from pathlib import Path


def load_eval_module():
    spec = importlib.util.spec_from_file_location("run_eval", "/repo/eval/run_eval.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    evaluation = load_eval_module()
    passing = {"recall@5": 0.90, "mrr": 0.75, "ndcg@10": 0.70, "forbidden@10": 0}
    assert evaluation.gate_failures(passing, 299.999) == []
    assert evaluation.gate_failures(passing, 300.0) == ["p95_latency_ms"]
    assert evaluation.gate_failures({**passing, "recall@5": 0.89}, 301.0) == [
        "recall@5", "p95_latency_ms"]
    print("3/3 passed")


if __name__ == "__main__":
    main()
