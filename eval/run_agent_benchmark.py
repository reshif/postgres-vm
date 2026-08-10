"""Run or record Suite 7's five-arm end-to-end benchmark.

An agent runner writes observations separately. This script validates that every
task/arm/repetition is present, calculates the ADR-0014 scorecard, and records
the complete raw observation in ``evaluation_case_results``. It never fills a
missing cell with an estimate.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from benchmark import BenchmarkError, load_json, report, validate_manifest, validate_observation

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "eval" / "agent_benchmark.json"
TENANT = UUID("ea1a0000-0000-0000-0000-00000000e001")
PROJECT = UUID("ea1a0000-0000-0000-0000-00000000e002")
PRINCIPAL = UUID("ea1a0000-0000-0000-0000-00000000e003")


def load_observations(path: Path, manifest: dict) -> list[dict]:
    payload = load_json(path)
    if payload.get("version") != 1:
        raise BenchmarkError("observation file version must be 1")
    rows = payload.get("observations")
    if not isinstance(rows, list):
        raise BenchmarkError("observation file needs an observations list")
    for row in rows:
        if not isinstance(row, dict):
            raise BenchmarkError("every observation must be an object")
        validate_observation(row, manifest)
    return rows


def record(run: dict, manifest: dict, observations: list[dict]) -> dict:
    """Persist a transparent benchmark run in the existing scoped eval ledger."""
    sys.path.insert(0, "/app/src")
    from sqlalchemy import text
    from memory_platform import db, evaluation

    with db.engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO mem.organizations (id,slug,name) VALUES (:id,'eval','Eval') "
            "ON CONFLICT DO NOTHING"), {"id": str(TENANT)})
        conn.execute(text(
            "INSERT INTO mem.projects (id,tenant_id,slug,name) "
            "VALUES (:id,:tenant,'eval-corpus','Eval') ON CONFLICT DO NOTHING"),
            {"id": str(PROJECT), "tenant": str(TENANT)})
        conn.execute(text(
            "INSERT INTO mem.principals (id,tenant_id,actor,external_id,display_name) "
            "VALUES (:id,:tenant,'agent',:external,'Eval') ON CONFLICT DO NOTHING"),
            {"id": str(PRINCIPAL), "tenant": str(TENANT), "external": f"eval-{PRINCIPAL}"})

    prompts = {task["id"]: task["prompt"] for task in manifest["tasks"]}
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as conn:
        return evaluation.record_run(
            conn, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            suite="agent-benchmark", status=run["status"],
            corpus_snapshot=str(manifest.get("corpus_snapshot", "")),
            metrics={
                "task_count": run["task_count"], "repetitions": run["repetitions"],
                "headline_gate": run["headline_gate"], "capability_scorecard": run["capability_scorecard"],
            },
            configuration={"arms": manifest["arm_contract"], "manifest_version": manifest["version"],
                           "recorded_at": datetime.now(timezone.utc).isoformat()},
            cases=[{
                "case_id": f"{item['task_id']}:{item['arm']}:{item['repetition']}",
                "query_text": prompts[item["task_id"]], "status": run["status"], "result": item,
            } for item in observations],
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and score Suite 7 agent observations")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--record", action="store_true", help="append the run to evaluation history")
    args = parser.parse_args(argv)
    try:
        manifest = load_json(args.manifest)
        validate_manifest(manifest)
        if args.observations is None:
            print(f"valid Suite 7 manifest: {len(manifest['tasks'])} tasks x {len(manifest['arms'])} arms x 5 runs")
            return 0
        observations = load_observations(args.observations, manifest)
        outcome = report(manifest, observations)
        print(json.dumps(outcome, indent=2))
        if args.record:
            print(json.dumps({"recorded": record(outcome, manifest, observations)}, indent=2))
        return 0 if outcome["status"] == "passed" else 1
    except BenchmarkError as exc:
        print(f"invalid benchmark: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
