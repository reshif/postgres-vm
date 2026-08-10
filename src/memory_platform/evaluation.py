"""Durable evaluation evidence for CI and the read-only Evals console."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

STATUSES = {"passed", "failed", "incomplete"}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _run_row(row: Any) -> dict:
    return {
        "id": str(row["id"]), "suite": row["suite"], "status": row["status"],
        "corpus_snapshot": row["corpus_snapshot"], "ranking_profile": row["ranking_profile"],
        "source_commit": row["source_commit"], "metrics": dict(row["metrics"]),
        "configuration": dict(row["configuration"]), "started_at": _iso(row["started_at"]),
        "completed_at": _iso(row["completed_at"]), "created_at": _iso(row["created_at"]),
        "created_by": str(row["created_by"]) if row["created_by"] else None,
    }


def record_run(
    conn: Connection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    principal_id: UUID | None,
    suite: str,
    status: str,
    corpus_snapshot: str = "",
    ranking_profile: str | None = None,
    source_commit: str | None = None,
    metrics: dict[str, Any] | None = None,
    configuration: dict[str, Any] | None = None,
    cases: list[dict[str, Any]] | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> dict:
    """Append one completed or incomplete run with its per-case evidence."""
    cleaned_suite = suite.strip()
    if not 1 <= len(cleaned_suite) <= 100:
        raise ValueError("suite must be 1 to 100 characters")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {sorted(STATUSES)}")
    unique_case_ids: set[str] = set()
    for case in cases or []:
        case_id = str(case.get("case_id") or "").strip()
        case_status = str(case.get("status") or "")
        if not case_id or len(case_id) > 200:
            raise ValueError("each evaluation case needs a case_id of at most 200 characters")
        if case_id in unique_case_ids:
            raise ValueError(f"evaluation case is repeated: {case_id}")
        if case_status not in STATUSES:
            raise ValueError(f"case {case_id} has invalid status")
        if not isinstance(case.get("query_text"), str):
            raise ValueError(f"case {case_id} needs query_text")
        if not isinstance(case.get("result", {}), dict):
            raise ValueError(f"case {case_id} result must be an object")
        unique_case_ids.add(case_id)

    row = conn.execute(text(
        "INSERT INTO mem.evaluation_runs "
        "  (tenant_id, project_id, suite, status, corpus_snapshot, ranking_profile, "
        "   source_commit, metrics, configuration, started_at, completed_at, created_by) "
        "VALUES (:tenant, :project, :suite, :status, :snapshot, :profile, :commit, "
        "        CAST(:metrics AS jsonb), CAST(:configuration AS jsonb), "
        "        COALESCE(:started, now()), COALESCE(:completed, now()), :principal) "
        "RETURNING id, suite, status, corpus_snapshot, ranking_profile, source_commit, "
        "          metrics, configuration, started_at, completed_at, created_at, created_by"),
        {"tenant": str(tenant_id), "project": str(project_id), "suite": cleaned_suite,
         "status": status, "snapshot": corpus_snapshot[:200], "profile": ranking_profile,
         "commit": source_commit, "metrics": json.dumps(metrics or {}),
         "configuration": json.dumps(configuration or {}), "started": started_at,
         "completed": completed_at, "principal": str(principal_id) if principal_id else None},
    ).mappings().one()
    run_id = row["id"]
    for case in cases or []:
        conn.execute(text(
            "INSERT INTO mem.evaluation_case_results "
            "  (run_id, tenant_id, project_id, case_id, query_text, status, result) "
            "VALUES (:run, :tenant, :project, :case, :query, :status, CAST(:result AS jsonb))"),
            {"run": str(run_id), "tenant": str(tenant_id), "project": str(project_id),
             "case": str(case["case_id"]).strip(), "query": case["query_text"],
             "status": case["status"], "result": json.dumps(case.get("result", {}))})
    return _run_row(row) | {"case_count": len(cases or [])}


def list_runs(
    conn: Connection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    suite: str | None = None,
    limit: int = 50,
) -> list[dict]:
    rows = conn.execute(text(
        "SELECT r.id, r.suite, r.status, r.corpus_snapshot, r.ranking_profile, r.source_commit, "
        "       r.metrics, r.configuration, r.started_at, r.completed_at, r.created_at, r.created_by, "
        "       count(c.id)::int AS case_count "
        "  FROM mem.evaluation_runs r "
        "  LEFT JOIN mem.evaluation_case_results c ON c.run_id = r.id "
        " WHERE r.tenant_id = :tenant AND r.project_id = :project "
        "   AND (:suite = '' OR r.suite = :suite) "
        " GROUP BY r.id "
        " ORDER BY r.completed_at DESC NULLS LAST, r.created_at DESC LIMIT :limit"),
        {"tenant": str(tenant_id), "project": str(project_id), "suite": (suite or "").strip(),
         "limit": max(1, min(limit, 200))},
    ).mappings().all()
    return [_run_row(row) | {"case_count": row["case_count"]} for row in rows]


def get_run(
    conn: Connection, *, tenant_id: UUID, project_id: UUID, run_id: UUID,
) -> dict | None:
    run = conn.execute(text(
        "SELECT id, suite, status, corpus_snapshot, ranking_profile, source_commit, metrics, "
        "       configuration, started_at, completed_at, created_at, created_by "
        "  FROM mem.evaluation_runs "
        " WHERE id = :id AND tenant_id = :tenant AND project_id = :project"),
        {"id": str(run_id), "tenant": str(tenant_id), "project": str(project_id)},
    ).mappings().one_or_none()
    if run is None:
        return None
    cases = conn.execute(text(
        "SELECT case_id, query_text, status, result, created_at "
        "  FROM mem.evaluation_case_results "
        " WHERE run_id = :run AND tenant_id = :tenant AND project_id = :project "
        " ORDER BY case_id"),
        {"run": str(run_id), "tenant": str(tenant_id), "project": str(project_id)},
    ).mappings().all()
    return _run_row(run) | {"cases": [
        {"case_id": row["case_id"], "query_text": row["query_text"],
         "status": row["status"], "result": dict(row["result"]),
         "created_at": _iso(row["created_at"])}
        for row in cases
    ]}
