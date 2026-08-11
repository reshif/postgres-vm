"""Context-pack evaluation gates reject unsupported or unsafe results."""
from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path


def load_module():
    spec = importlib.util.spec_from_file_location("context_eval", "/repo/eval/run_context_eval.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    evaluation = load_module()
    passing = {
        "evidence_recall": 0.90,
        "no_evidence_precision": 1.0,
        "forbidden_in_pack": 0,
    }
    assert evaluation.gate_failures(passing, 299.9) == []
    assert evaluation.gate_failures({**passing, "evidence_recall": 0.89}, 299.9) == [
        "evidence_recall"]
    assert evaluation.gate_failures({**passing, "forbidden_in_pack": 1}, 300.0) == [
        "forbidden_in_pack", "p95_latency_ms"]

    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "ood.json"
        path.write_text(json.dumps({"version": 1, "cases": [
            {"id": "n01", "query": "unrelated question"},
        ]}), encoding="utf-8")
        assert evaluation.load_no_evidence_cases(path)[0]["id"] == "n01"
        path.write_text(json.dumps({"version": 1, "cases": [
            {"id": "n01", "query": "one"}, {"id": "n01", "query": "two"},
        ]}), encoding="utf-8")
        try:
            evaluation.load_no_evidence_cases(path)
        except ValueError:
            pass
        else:
            raise AssertionError("duplicate no-evidence ids must fail")
    print("5/5 passed")


if __name__ == "__main__":
    main()
