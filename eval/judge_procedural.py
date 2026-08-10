"""Apply the pinned Suite 6 Ollama judge to retained agent outputs.

Input observations intentionally have no ``judge`` object. This adapter adds
one without changing agent output, then ``run_procedural_eval.py`` validates and
records the resulting evidence. The raw model response is retained verbatim.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from procedural import ProceduralEvalError, load_json, prompt_sha256, validate_cases, validate_judge

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "eval" / "procedural_cases.json"
DEFAULT_JUDGE = ROOT / "eval" / "procedural_judge.json"
DEFAULT_URL = os.environ.get("MEMORY_LLM_URL", "http://127.0.0.1:11434").rstrip("/")


def request_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url, method="POST", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            value = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ProceduralEvalError(f"pinned judge request failed: {exc}") from exc
    if not isinstance(value, dict):
        raise ProceduralEvalError("pinned judge returned a non-object response")
    return value


def judge_prompt(case: dict[str, Any], agent_output: str, judge: dict[str, Any]) -> str:
    return "\n\n".join((
        judge["prompt"],
        "PROCEDURE CASE:\n" + json.dumps({
            "prompt": case["prompt"], "procedure_key": case["procedure_key"],
            "preconditions": case["preconditions"], "failure_modes": case["failure_modes"],
        }, indent=2),
        "AGENT OUTPUT:\n" + agent_output,
    ))


def parse_judgement(raw_output: str, judge: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        raise ProceduralEvalError(f"pinned judge did not return JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProceduralEvalError("pinned judge JSON must be an object")
    required = set(judge["response_schema"])
    if set(value) != required:
        raise ProceduralEvalError("pinned judge response keys do not match the versioned schema")
    return value


def judge_observation(
    observation: dict[str, Any], case: dict[str, Any], judge: dict[str, Any],
    *, url: str = DEFAULT_URL, transport=request_json,
) -> dict[str, Any]:
    if not isinstance(observation.get("agent_output"), str) or not observation["agent_output"].strip():
        raise ProceduralEvalError("judge input must retain a non-empty agent_output")
    response = transport(url + "/api/generate", {
        "model": judge["model"], "prompt": judge_prompt(case, observation["agent_output"], judge),
        "stream": False, "format": "json", "options": {"temperature": 0},
    })
    raw_output = response.get("response")
    if not isinstance(raw_output, str) or not raw_output.strip():
        raise ProceduralEvalError("pinned judge returned no response text")
    parsed = parse_judgement(raw_output, judge)
    scores = {key: parsed[key] for key in (
        "steps_followed", "preconditions_checked", "failure_modes_handled", "overall")}
    return {**observation, "judge": {
        "model": judge["model"], "model_digest": judge["model_digest"],
        "prompt_sha256": prompt_sha256(judge), "raw_output": raw_output,
        "scores": scores, "verdict": parsed["verdict"], "reasoning": parsed["reasoning"],
    }}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply the pinned Suite 6 Ollama judge")
    parser.add_argument("input", type=Path, help="version 1 JSON with unjudged observations")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--judge", type=Path, default=DEFAULT_JUDGE)
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args(argv)
    try:
        cases, judge = load_json(args.cases), load_json(args.judge)
        validate_cases(cases)
        validate_judge(judge)
        payload = load_json(args.input)
        rows = payload.get("observations")
        if payload.get("version") != 1 or not isinstance(rows, list):
            raise ProceduralEvalError("judge input needs version 1 and observations")
        by_id = {case["id"]: case for case in cases["cases"]}
        judged = []
        for observation in rows:
            if not isinstance(observation, dict) or observation.get("case_id") not in by_id:
                raise ProceduralEvalError("judge input includes an unknown procedure case")
            judged.append(judge_observation(observation, by_id[observation["case_id"]], judge, url=args.url))
        args.output.write_text(json.dumps({"version": 1, "observations": judged}, indent=2) + "\n", encoding="utf-8")
        print(f"judged {len(judged)} procedure observation(s) with {judge['model']}@{judge['model_digest']}")
        return 0
    except ProceduralEvalError as exc:
        print(f"procedural judge failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
