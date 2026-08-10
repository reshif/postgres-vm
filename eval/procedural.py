"""Suite 6 procedural-evaluation contracts and fixed-judge scoring helpers."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from statistics import mean, pstdev
from typing import Any


class ProceduralEvalError(ValueError):
    """The procedural evaluation input is not replayable evidence."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProceduralEvalError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProceduralEvalError(f"{path} must contain a JSON object")
    return value


def prompt_sha256(judge: dict[str, Any]) -> str:
    prompt = judge.get("prompt")
    if not isinstance(prompt, str):
        raise ProceduralEvalError("judge prompt must be a string")
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _score(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProceduralEvalError(f"{field} must be a finite number")
    if not 0 <= value <= 1:
        raise ProceduralEvalError(f"{field} must be between 0 and 1")
    return float(value)


def validate_judge(judge: dict[str, Any]) -> None:
    if judge.get("version") != 1:
        raise ProceduralEvalError("judge version must be 1")
    for field in ("provider", "model", "model_digest", "prompt_version", "prompt"):
        if not isinstance(judge.get(field), str) or not judge[field].strip():
            raise ProceduralEvalError(f"judge {field} must be a non-empty string")
    if judge.get("temperature") != 0:
        raise ProceduralEvalError("procedural judge temperature must be pinned to 0")
    schema = judge.get("response_schema")
    required = {"steps_followed", "preconditions_checked", "failure_modes_handled", "overall", "verdict", "reasoning"}
    if not isinstance(schema, dict) or set(schema) != required:
        raise ProceduralEvalError("judge response_schema must declare every required response key")


def validate_cases(cases: dict[str, Any]) -> None:
    if cases.get("version") != 1 or cases.get("repetitions") != 5 or cases.get("arms") != ["B", "D"]:
        raise ProceduralEvalError("Suite 6 must use version 1, five runs, and B/D arms")
    rows = cases.get("cases")
    if not isinstance(rows, list) or len(rows) < 4:
        raise ProceduralEvalError("Suite 6 needs at least four procedure cases")
    ids: set[str] = set()
    for case in rows:
        if not isinstance(case, dict):
            raise ProceduralEvalError("every procedure case must be an object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id or case_id in ids:
            raise ProceduralEvalError(f"procedure case id is missing or repeated: {case_id!r}")
        ids.add(case_id)
        for field in ("procedure_key", "prompt"):
            if not isinstance(case.get(field), str) or not case[field].strip():
                raise ProceduralEvalError(f"procedure case {case_id} needs {field}")
        for field in ("preconditions", "failure_modes"):
            if not isinstance(case.get(field), list) or not case[field] or not all(
                    isinstance(item, str) and item for item in case[field]):
                raise ProceduralEvalError(f"procedure case {case_id} needs {field}")


def validate_observation(observation: dict[str, Any], cases: dict[str, Any], judge: dict[str, Any]) -> None:
    case_ids = {case["id"] for case in cases["cases"]}
    if observation.get("case_id") not in case_ids:
        raise ProceduralEvalError("procedure observation has an unknown case_id")
    if observation.get("arm") not in cases["arms"]:
        raise ProceduralEvalError("procedure observation has an unknown arm")
    if not isinstance(observation.get("repetition"), int) or not 1 <= observation["repetition"] <= 5:
        raise ProceduralEvalError("procedure observation needs repetition 1 through 5")
    if not isinstance(observation.get("agent_output"), str) or not observation["agent_output"].strip():
        raise ProceduralEvalError("procedure observation must retain the full agent_output")
    outcome = observation.get("judge")
    if not isinstance(outcome, dict):
        raise ProceduralEvalError("procedure observation needs judge output")
    if outcome.get("model") != judge["model"] or outcome.get("model_digest") != judge["model_digest"]:
        raise ProceduralEvalError("judge model does not match the pinned model")
    if outcome.get("prompt_sha256") != prompt_sha256(judge):
        raise ProceduralEvalError("judge prompt fingerprint does not match the pinned prompt")
    if not isinstance(outcome.get("raw_output"), str) or not outcome["raw_output"].strip():
        raise ProceduralEvalError("full raw judge output is required")
    scores = outcome.get("scores")
    if not isinstance(scores, dict):
        raise ProceduralEvalError("judge scores must be an object")
    for field in ("steps_followed", "preconditions_checked", "failure_modes_handled", "overall"):
        _score(scores.get(field), f"judge scores.{field}")
    if outcome.get("verdict") not in {"passed", "failed"}:
        raise ProceduralEvalError("judge verdict must be passed or failed")
    if not isinstance(outcome.get("reasoning"), str) or not outcome["reasoning"].strip():
        raise ProceduralEvalError("judge reasoning is required")


def coverage(cases: dict[str, Any], observations: list[dict[str, Any]], judge: dict[str, Any]) -> dict[str, Any]:
    validate_cases(cases)
    validate_judge(judge)
    seen: Counter[tuple[str, str, int]] = Counter()
    invalid: list[str] = []
    for index, observation in enumerate(observations):
        try:
            validate_observation(observation, cases, judge)
        except ProceduralEvalError as exc:
            invalid.append(f"#{index + 1}: {exc}")
            continue
        seen[(observation["case_id"], observation["arm"], observation["repetition"])] += 1
    expected = {(case["id"], arm, repetition) for case in cases["cases"]
                for arm in cases["arms"] for repetition in range(1, 6)}
    missing = sorted(expected - set(seen))
    duplicate = sorted(key for key, count in seen.items() if count > 1)
    return {"expected": len(expected), "observed": len(observations), "missing": missing,
            "duplicate": duplicate, "invalid": invalid,
            "complete": not (missing or duplicate or invalid)}


def report(cases: dict[str, Any], observations: list[dict[str, Any]], judge: dict[str, Any]) -> dict[str, Any]:
    state = coverage(cases, observations, judge)
    arms: dict[str, Any] = {}
    for arm in cases["arms"]:
        rows = [item for item in observations if item.get("arm") == arm and isinstance(item.get("judge"), dict)]
        metrics = {}
        for field in ("steps_followed", "preconditions_checked", "failure_modes_handled", "overall"):
            values = [float(item["judge"]["scores"][field]) for item in rows
                      if isinstance(item["judge"].get("scores"), dict) and field in item["judge"]["scores"]]
            metrics[field] = {"mean": round(mean(values), 6) if values else None,
                              "stddev": round(pstdev(values), 6) if values else None}
        arms[arm] = metrics
    baseline, full = arms["B"]["overall"]["mean"], arms["D"]["overall"]["mean"]
    advantage = None if baseline is None or full is None else full - baseline
    status = "incomplete" if not state["complete"] else ("passed" if advantage is not None and advantage >= 0.25 else "failed")
    return {"suite": "procedural-usefulness", "status": status, "coverage": state,
            "judge": {"provider": judge["provider"], "model": judge["model"],
                      "model_digest": judge["model_digest"], "prompt_version": judge["prompt_version"],
                      "prompt_sha256": prompt_sha256(judge)},
            "arms": arms, "procedure_advantage_d_vs_b": advantage}
