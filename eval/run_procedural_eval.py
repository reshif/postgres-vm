"""Validate, score, and persist Suite 6 LLM-judged procedure observations."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from procedural import ProceduralEvalError, load_json, report, validate_cases, validate_judge, validate_observation

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "eval" / "procedural_cases.json"
DEFAULT_JUDGE = ROOT / "eval" / "procedural_judge.json"
TENANT = UUID("ea1a0000-0000-0000-0000-00000000e001")
PROJECT = UUID("ea1a0000-0000-0000-0000-00000000e002")
PRINCIPAL = UUID("ea1a0000-0000-0000-0000-00000000e003")


def load_observations(path: Path, cases: dict, judge: dict) -> list[dict]:
    payload = load_json(path)
    if payload.get("version") != 1 or not isinstance(payload.get("observations"), list):
        raise ProceduralEvalError("observation file needs version 1 and an observations list")
    for item in payload["observations"]:
        if not isinstance(item, dict):
            raise ProceduralEvalError("every procedure observation must be an object")
        validate_observation(item, cases, judge)
    return payload["observations"]


def record(run: dict, cases: dict, observations: list[dict]) -> dict:
    sys.path.insert(0, "/app/src")
    from sqlalchemy import text
    from memory_platform import db, evaluation

    with db.engine().begin() as conn:
        conn.execute(text("INSERT INTO mem.organizations (id,slug,name) VALUES (:id,'eval','Eval') ON CONFLICT DO NOTHING"), {"id": str(TENANT)})
        conn.execute(text("INSERT INTO mem.projects (id,tenant_id,slug,name) VALUES (:id,:tenant,'eval-corpus','Eval') ON CONFLICT DO NOTHING"), {"id": str(PROJECT), "tenant": str(TENANT)})
        conn.execute(text("INSERT INTO mem.principals (id,tenant_id,actor,external_id,display_name) VALUES (:id,:tenant,'agent',:external,'Eval') ON CONFLICT DO NOTHING"), {"id": str(PRINCIPAL), "tenant": str(TENANT), "external": f"eval-{PRINCIPAL}"})
    prompts = {case["id"]: case["prompt"] for case in cases["cases"]}
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as conn:
        return evaluation.record_run(
            conn, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            suite="procedural-usefulness", status=run["status"],
            metrics={"procedure_advantage_d_vs_b": run["procedure_advantage_d_vs_b"],
                     "judge": run["judge"], "arms": run["arms"]},
            configuration={"judge": run["judge"], "recorded_at": datetime.now(timezone.utc).isoformat()},
            cases=[{"case_id": f"{item['case_id']}:{item['arm']}:{item['repetition']}",
                    "query_text": prompts[item["case_id"]], "status": run["status"], "result": item}
                   for item in observations],
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score Suite 6 procedural observations")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--judge", type=Path, default=DEFAULT_JUDGE)
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--record", action="store_true")
    args = parser.parse_args(argv)
    try:
        cases, judge = load_json(args.cases), load_json(args.judge)
        validate_cases(cases)
        validate_judge(judge)
        if args.observations is None:
            print(f"valid Suite 6 contract: {len(cases['cases'])} cases x {len(cases['arms'])} arms x 5 runs")
            return 0
        observations = load_observations(args.observations, cases, judge)
        outcome = report(cases, observations, judge)
        print(json.dumps(outcome, indent=2))
        if args.record:
            print(json.dumps({"recorded": record(outcome, cases, observations)}, indent=2))
        return 0 if outcome["status"] == "passed" else 1
    except ProceduralEvalError as exc:
        print(f"invalid procedural evaluation: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
