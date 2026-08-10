"""Suite 6 keeps its pinned judge and full raw judgement evidence."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def load_procedural():
    spec = importlib.util.spec_from_file_location("procedural", "/repo/eval/procedural.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def observation(case: dict, arm: str, repetition: int, judge: dict, procedures) -> dict:
    baseline = arm == "B"
    scores = {"steps_followed": 0.5 if baseline else 0.8,
              "preconditions_checked": 0.5 if baseline else 0.8,
              "failure_modes_handled": 0.5 if baseline else 0.8,
              "overall": 0.5 if baseline else 0.8}
    return {"case_id": case["id"], "arm": arm, "repetition": repetition,
            "agent_output": "The agent's complete procedure execution is retained here.",
            "judge": {"model": judge["model"], "model_digest": judge["model_digest"],
                      "prompt_sha256": procedures.prompt_sha256(judge),
                      "raw_output": json.dumps({"scores": scores, "verdict": "passed"}),
                      "scores": scores, "verdict": "passed", "reasoning": "All required controls were checked."}}


def main() -> None:
    procedures = load_procedural()
    cases = json.loads(Path("/repo/eval/procedural_cases.json").read_text("utf-8"))
    judge = json.loads(Path("/repo/eval/procedural_judge.json").read_text("utf-8"))
    procedures.validate_cases(cases)
    procedures.validate_judge(judge)
    rows = [observation(case, arm, repetition, judge, procedures) for case in cases["cases"]
            for arm in cases["arms"] for repetition in range(1, 6)]
    result = procedures.report(cases, rows, judge)
    assert result["coverage"]["complete"] and result["status"] == "passed"
    assert result["judge"]["model_digest"] == "a80c4f17acd5"
    bad = {**rows[0], "judge": {**rows[0]["judge"], "raw_output": ""}}
    try:
        procedures.validate_observation(bad, cases, judge)
    except procedures.ProceduralEvalError:
        pass
    else:
        raise AssertionError("full raw judge output must be required")
    assert procedures.report(cases, rows[:-1], judge)["status"] == "incomplete"
    print("4/4 passed")


if __name__ == "__main__":
    main()
