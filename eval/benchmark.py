"""Suite 7 benchmark contract and scorecard calculations.

The benchmark runner deliberately accepts observations from an external agent
runner instead of fabricating an agent in-process. The runner is part of the
measurement: the execution environment, model and tool permissions must be
recorded alongside each result so Arm D is compared with Arm B honestly.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, pstdev
from typing import Any


ARMS = ("A", "B", "C", "D", "E")
CAPABILITIES = ("C1", "C2", "C3", "C4", "C5", "C6")
TASK_CLASSES = {
    "rationale", "recurrence", "temporal", "authority", "isolation",
    "procedure", "onboarding", "impact", "anti_repetition",
}
CAPABILITY_MEASURES = {
    "C1": ("rubric_accuracy", "provenance_correct"),
    "C2": ("turns_to_completion",),
    "C3": ("temporal_correct",),
    "C4": ("trust_attribution_correct",),
    "C5": ("leakage",),
    "C6": ("procedure_steps_followed", "preconditions_checked"),
}
REQUIRED_OBSERVATION_FIELDS = {
    "task_success", "turns_to_completion", "total_tokens", "memory_tokens",
    "wall_clock_ms", "repeated_questions", "repeated_failed_approaches",
    "incorrect_actions", "evidence_keys", "capability_metrics",
}


class BenchmarkError(ValueError):
    """A benchmark manifest or result is not durable measurement evidence."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BenchmarkError(f"{path} must contain a JSON object")
    return value


def _number(value: Any, field: str, *, minimum: float = 0.0, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise BenchmarkError(f"{field} must be a finite number")
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"between {minimum} and {maximum}" if maximum is not None else f">= {minimum}"
        raise BenchmarkError(f"{field} must be {bound}")
    return float(value)


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("version") != 1:
        raise BenchmarkError("benchmark manifest version must be 1")
    if not isinstance(manifest.get("repository"), str) or not manifest["repository"].strip():
        raise BenchmarkError("benchmark manifest needs a repository")
    if manifest.get("repetitions") != 5:
        raise BenchmarkError("Suite 7 requires exactly five repetitions per task and arm")
    if manifest.get("arms") != list(ARMS):
        raise BenchmarkError("benchmark arms must be A, B, C, D, E in that order")

    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or not 20 <= len(tasks) <= 40:
        raise BenchmarkError("Suite 7 needs 20 to 40 real repository tasks")
    ids: set[str] = set()
    prompts: set[str] = set()
    coverage = Counter()
    for task in tasks:
        if not isinstance(task, dict):
            raise BenchmarkError("every task must be an object")
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id or task_id in ids:
            raise BenchmarkError(f"task id is missing or repeated: {task_id!r}")
        ids.add(task_id)
        capability = task.get("capability")
        if capability not in CAPABILITIES:
            raise BenchmarkError(f"task {task_id} has an unknown capability")
        coverage[capability] += 1
        if task.get("task_class") not in TASK_CLASSES:
            raise BenchmarkError(f"task {task_id} has an unknown task class")
        prompt = task.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or prompt in prompts:
            raise BenchmarkError(f"task {task_id} needs a unique non-empty prompt")
        prompts.add(prompt)
        expected = task.get("expected_evidence")
        if not isinstance(expected, list) or not expected or not all(
                isinstance(key, str) and key for key in expected):
            raise BenchmarkError(f"task {task_id} needs stable expected_evidence keys")
        scorer = task.get("scorer")
        if not isinstance(scorer, dict) or scorer.get("kind") not in {"deterministic", "rubric"}:
            raise BenchmarkError(f"task {task_id} needs a deterministic or rubric scorer")
    missing_capabilities = [capability for capability in CAPABILITIES if not coverage[capability]]
    if missing_capabilities:
        raise BenchmarkError("task manifest misses capabilities: " + ", ".join(missing_capabilities))


def validate_observation(observation: dict[str, Any], manifest: dict[str, Any]) -> None:
    task_ids = {task["id"] for task in manifest["tasks"]}
    task_id = observation.get("task_id")
    if task_id not in task_ids:
        raise BenchmarkError(f"observation has unknown task_id: {task_id!r}")
    if observation.get("arm") not in ARMS:
        raise BenchmarkError(f"observation {task_id} has an unknown arm")
    if not isinstance(observation.get("repetition"), int) or not 1 <= observation["repetition"] <= 5:
        raise BenchmarkError(f"observation {task_id} needs repetition 1 through 5")
    missing = REQUIRED_OBSERVATION_FIELDS - set(observation)
    if missing:
        raise BenchmarkError(f"observation {task_id} is missing: {', '.join(sorted(missing))}")
    if not isinstance(observation["task_success"], bool):
        raise BenchmarkError(f"observation {task_id}.task_success must be boolean")
    for field in ("turns_to_completion", "total_tokens", "memory_tokens", "wall_clock_ms",
                  "repeated_questions", "repeated_failed_approaches", "incorrect_actions"):
        _number(observation[field], f"observation {task_id}.{field}")
    if observation["memory_tokens"] > observation["total_tokens"]:
        raise BenchmarkError(f"observation {task_id}.memory_tokens exceeds total_tokens")
    if not isinstance(observation["evidence_keys"], list) or not all(
            isinstance(key, str) and key for key in observation["evidence_keys"]):
        raise BenchmarkError(f"observation {task_id}.evidence_keys must be stable keys")
    metrics = observation["capability_metrics"]
    if not isinstance(metrics, dict):
        raise BenchmarkError(f"observation {task_id}.capability_metrics must be an object")
    task = next(task for task in manifest["tasks"] if task["id"] == task_id)
    for field in CAPABILITY_MEASURES[task["capability"]]:
        if field == "turns_to_completion":
            continue
        _number(metrics.get(field), f"observation {task_id}.{field}", maximum=1.0)


def coverage(manifest: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    validate_manifest(manifest)
    seen: Counter[tuple[str, str, int]] = Counter()
    invalid: list[str] = []
    for index, observation in enumerate(observations):
        try:
            validate_observation(observation, manifest)
        except BenchmarkError as exc:
            invalid.append(f"#{index + 1}: {exc}")
            continue
        seen[(observation["task_id"], observation["arm"], observation["repetition"])] += 1
    expected = {
        (task["id"], arm, repetition)
        for task in manifest["tasks"] for arm in ARMS for repetition in range(1, 6)
    }
    duplicate = sorted(key for key, count in seen.items() if count > 1)
    unexpected = sorted(key for key in seen if key not in expected)
    missing = sorted(expected - set(seen))
    return {
        "expected": len(expected), "observed": len(observations), "missing": missing,
        "duplicate": duplicate, "invalid": invalid, "unexpected": unexpected,
        "complete": not (missing or duplicate or invalid or unexpected),
    }


def _mean_std(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "stddev": None}
    return {"mean": round(mean(values), 6), "stddev": round(pstdev(values), 6)}


def _observations_for(
    manifest: dict[str, Any], observations: list[dict[str, Any]], capability: str, arm: str,
) -> list[dict[str, Any]]:
    task_ids = {task["id"] for task in manifest["tasks"] if task["capability"] == capability}
    return [item for item in observations if item["task_id"] in task_ids and item["arm"] == arm]


def _reduction(baseline: float, improved: float) -> float | None:
    return None if baseline <= 0 else (baseline - improved) / baseline


def capability_scorecard(manifest: dict[str, Any], observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for capability in CAPABILITIES:
        baseline = _observations_for(manifest, observations, capability, "B")
        full = _observations_for(manifest, observations, capability, "D")
        if not baseline or not full:
            cards.append({"capability": capability, "status": "incomplete"})
            continue
        metric = lambda items, key: mean(float(item["capability_metrics"].get(key, 0)) for item in items)
        success_b = mean(float(item["task_success"]) for item in baseline)
        success_d = mean(float(item["task_success"]) for item in full)
        if capability == "C1":
            accuracy_b, accuracy_d = metric(baseline, "rubric_accuracy"), metric(full, "rubric_accuracy")
            provenance_d = metric(full, "provenance_correct")
            passed = accuracy_d - accuracy_b >= 0.20 and provenance_d >= 0.90
            detail = {"accuracy_b": accuracy_b, "accuracy_d": accuracy_d, "provenance_d": provenance_d}
        elif capability == "C2":
            turns_b = mean(float(item["turns_to_completion"]) for item in baseline)
            turns_d = mean(float(item["turns_to_completion"]) for item in full)
            passed = _reduction(turns_b, turns_d) is not None and _reduction(turns_b, turns_d) >= 0.30 and success_d >= success_b
            detail = {"turns_b": turns_b, "turns_d": turns_d, "success_b": success_b, "success_d": success_d}
        elif capability == "C3":
            temporal_d = metric(full, "temporal_correct")
            passed, detail = temporal_d >= 0.90, {"temporal_d": temporal_d}
        elif capability == "C4":
            trust_d = metric(full, "trust_attribution_correct")
            passed, detail = trust_d >= 0.95, {"trust_d": trust_d}
        elif capability == "C5":
            leakage_d = metric(full, "leakage")
            passed, detail = leakage_d == 0, {"leakage_d": leakage_d}
        else:
            procedure_b = mean((metric(baseline, "procedure_steps_followed"), metric(baseline, "preconditions_checked")))
            procedure_d = mean((metric(full, "procedure_steps_followed"), metric(full, "preconditions_checked")))
            passed = procedure_d - procedure_b >= 0.25
            detail = {"procedure_b": procedure_b, "procedure_d": procedure_d}
        cards.append({"capability": capability, "status": "passed" if passed else "failed", **detail})
    return cards


def headline_gate(observations: list[dict[str, Any]]) -> dict[str, Any]:
    baseline = [item for item in observations if item["arm"] == "B"]
    full = [item for item in observations if item["arm"] == "D"]
    if not baseline or not full:
        return {"status": "incomplete", "passed_count": 0, "criteria": []}
    average = lambda items, field: mean(float(item[field]) for item in items)
    comparisons = [
        ("repeated_questions", _reduction(average(baseline, "repeated_questions"), average(full, "repeated_questions")), 0.40, "reduction"),
        ("turns_to_completion", _reduction(average(baseline, "turns_to_completion"), average(full, "turns_to_completion")), 0.15, "reduction"),
        ("task_success", average(full, "task_success") - average(baseline, "task_success"), 0.10, "increase"),
        ("total_tokens", _reduction(average(baseline, "total_tokens"), average(full, "total_tokens")), -0.10, "reduction"),
        ("repeated_failed_approaches", _reduction(average(baseline, "repeated_failed_approaches"), average(full, "repeated_failed_approaches")), 0.50, "reduction"),
    ]
    criteria = []
    for name, observed, threshold, direction in comparisons:
        measurable = observed is not None
        criteria.append({"metric": name, "observed": observed, "threshold": threshold,
                         "measurable": measurable, "passed": measurable and observed >= threshold,
                         "direction": direction})
    passed_count = sum(item["passed"] for item in criteria)
    return {"status": "passed" if passed_count >= 3 else "failed", "passed_count": passed_count,
            "required": 3, "criteria": criteria}


def report(manifest: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    state = coverage(manifest, observations)
    arm_metrics: dict[str, Any] = {}
    for arm in ARMS:
        arm_rows = [item for item in observations if item.get("arm") == arm]
        arm_metrics[arm] = {
            field: _mean_std([float(item[field]) for item in arm_rows])
            for field in ("task_success", "turns_to_completion", "total_tokens", "memory_tokens",
                          "wall_clock_ms", "repeated_questions", "repeated_failed_approaches",
                          "incorrect_actions")
        }
    cards = capability_scorecard(manifest, observations)
    headline = headline_gate(observations)
    status = "incomplete" if not state["complete"] else headline["status"]
    return {
        "suite": "agent-benchmark", "status": status,
        "repository": manifest["repository"], "task_count": len(manifest["tasks"]),
        "repetitions": manifest["repetitions"], "coverage": state, "arms": arm_metrics,
        "capability_scorecard": cards, "headline_gate": headline,
    }
