"""Evaluation evidence is durable, scoped, and visible through the API.

    docker compose exec -T api python - < tests/test_evaluations.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import db  # noqa: E402

API = "http://localhost:8080"
RUN = uuid.uuid4().hex[:8]
TENANT = UUID(f"e1a1{RUN[:4]}-0000-4000-8000-000000000001")
PROJECT = UUID(f"e1a1{RUN[:4]}-0000-4000-8000-000000000002")
PRINCIPAL = UUID(f"e1a1{RUN[:4]}-0000-4000-8000-000000000003")
OTHER_TENANT = UUID(f"e1a1{RUN[:4]}-0000-4000-8000-000000000004")
OTHER_PROJECT = UUID(f"e1a1{RUN[:4]}-0000-4000-8000-000000000005")

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def request_json(method: str, path: str, body: dict | None = None, **params) -> tuple[int, dict]:
    query = urllib.parse.urlencode({key: str(value) for key, value in params.items()
                                    if value is not None})
    request = urllib.request.Request(
        f"{API}{path}?{query}", method=method,
        data=json.dumps(body, default=str).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.status, json.load(response)


def seed() -> None:
    with db.engine().begin() as conn:
        for tenant, slug in ((TENANT, f"eval-ui-{RUN}"),
                             (OTHER_TENANT, f"eval-ui-other-{RUN}")):
            conn.execute(text(
                "INSERT INTO mem.organizations (id, slug, name) VALUES (:id, :slug, :slug) "
                "ON CONFLICT DO NOTHING"), {"id": str(tenant), "slug": slug})
        for project, tenant, slug in ((PROJECT, TENANT, f"eval-ui-{RUN}"),
                                      (OTHER_PROJECT, OTHER_TENANT, f"eval-ui-other-{RUN}")):
            conn.execute(text(
                "INSERT INTO mem.projects (id, tenant_id, slug, name) "
                "VALUES (:id, :tenant, :slug, :slug) ON CONFLICT DO NOTHING"),
                {"id": str(project), "tenant": str(tenant), "slug": slug})
        conn.execute(text(
            "INSERT INTO mem.principals (id, tenant_id, actor, external_id, display_name) "
            "VALUES (:id, :tenant, 'service', :external, 'Evaluation CI') ON CONFLICT DO NOTHING"),
            {"id": str(PRINCIPAL), "tenant": str(TENANT), "external": f"eval-ui-{RUN}"})


def main() -> None:
    seed()
    scope = {"tenant_id": TENANT, "project_id": PROJECT, "principal_id": PRINCIPAL}

    print("\n1. CI run record")
    payload = {
        **scope, "suite": "retrieval-accuracy", "status": "failed",
        "corpus_snapshot": "snapshot-2026-08", "ranking_profile": "default@2",
        "source_commit": "abc123def456",
        "metrics": {"recall@5": 0.8, "mrr": 0.699, "forbidden@10": 0},
        "configuration": {"rerank": False, "embedding_model": "bge-m3@1"},
        "cases": [
            {"case_id": "g01", "query_text": "why is RLS forced?", "status": "passed",
             "result": {"recall@5": 1.0, "expected_positions": {"adr-0007": 1}}},
            {"case_id": "g02", "query_text": "what is the queue?", "status": "failed",
             "result": {"recall@5": 0.0, "expected_positions": {"adr-0010": None}}},
        ],
    }
    status, created = request_json("POST", "/v1/evals/runs", payload)
    run_id = created.get("id")
    check("a CI run stores its metrics and per-case evidence",
          status == 201 and bool(run_id) and created["case_count"] == 2
          and created["metrics"] == payload["metrics"], str(created))

    print("\n2. Trend and run detail")
    _, listed = request_json("GET", "/v1/evals", **scope)
    check("the project trend lists the recorded run",
          any(run["id"] == run_id and run["case_count"] == 2 for run in listed["runs"]),
          str(listed))
    _, detailed = request_json("GET", f"/v1/evals/{run_id}", **scope)
    check("run detail exposes pass/fail case evidence",
          {case["case_id"] for case in detailed["cases"]} == {"g01", "g02"}
          and detailed["cases"][1]["result"]["recall@5"] == 0.0,
          str(detailed))

    print("\n3. Validation and isolation")
    invalid_status = False
    try:
        request_json("POST", "/v1/evals/runs", {**scope, "suite": "x", "status": "green"})
    except urllib.error.HTTPError as error:
        invalid_status = error.code == 422
    check("invalid evaluation statuses are rejected", invalid_status)

    hidden = False
    try:
        request_json("GET", f"/v1/evals/{run_id}", tenant_id=OTHER_TENANT,
                     project_id=OTHER_PROJECT, principal_id=OTHER_TENANT)
    except urllib.error.HTTPError as error:
        hidden = error.code == 404
    check("another project cannot read evaluation evidence", hidden)

    failed = [name for ok, name in results if not ok]
    print(f"\n{'=' * 62}\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
