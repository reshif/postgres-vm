"""The Suite 6 Ollama adapter retains and validates the complete judgement."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def load_module():
    sys.path.insert(0, "/repo/eval")
    spec = importlib.util.spec_from_file_location("judge_procedural", "/repo/eval/judge_procedural.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    adapter = load_module()
    judge = json.loads(Path("/repo/eval/procedural_judge.json").read_text("utf-8"))
    case = json.loads(Path("/repo/eval/procedural_cases.json").read_text("utf-8"))["cases"][0]
    raw = json.dumps({"steps_followed": 1.0, "preconditions_checked": 1.0,
                      "failure_modes_handled": 1.0, "overall": 1.0,
                      "verdict": "passed", "reasoning": "Every required step is present."})
    result = adapter.judge_observation(
        {"case_id": case["id"], "arm": "D", "repetition": 1, "agent_output": "Complete output."},
        case, judge, transport=lambda _url, _payload: {"response": raw})
    assert result["judge"]["raw_output"] == raw
    assert result["judge"]["scores"]["overall"] == 1.0
    try:
        adapter.parse_judgement("{}", judge)
    except adapter.ProceduralEvalError:
        pass
    else:
        raise AssertionError("judge schema drift must be rejected")
    print("3/3 passed")


if __name__ == "__main__":
    main()
