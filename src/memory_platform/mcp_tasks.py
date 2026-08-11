"""The MCP Tasks extension — long operations behind a durable handle.

02-MCP-CONTRACT.md: "Tasks extension (io.modelcontextprotocol/tasks) for long
operations: repository ingestion, re-embedding, consolidation runs, evaluation
runs. Poll with tasks/get; accept input with tasks/update."

The handle is a row, not an object in the gateway's memory. ADR-0004 requires
application state to be explicit, durable and attributable, and a dict would fail
all three — plus one practical thing the ADR does not have to spell out: the
gateway is horizontally scaled, so a poll for an in-process task reaches a
replica that never saw it created, and the client is told its task does not
exist.

WHAT IS AND IS NOT A TASK. Only work whose duration is unbounded from the
caller's point of view: ingesting a repository, re-embedding a corpus,
consolidation, an evaluation run. `memory.context` is emphatically not one — it
has a 350 ms production gate, and putting a hot path behind a poll loop would
turn a latency budget into a client-side retry loop.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

log = logging.getLogger("memory.mcp_tasks")

KINDS = ("ingest", "reembed", "consolidate", "evaluate")
TERMINAL = ("completed", "failed", "cancelled")


def create(conn: Connection, *, tenant_id: UUID, project_id: UUID,
           principal_id: UUID | None, kind: str,
           arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"unknown task kind: {kind}")
    row = conn.execute(
        text("INSERT INTO mem.mcp_tasks "
             "  (tenant_id, project_id, principal_id, kind, arguments) "
             "VALUES (:t, :p, :pr, :k, CAST(:a AS jsonb)) "
             "RETURNING id, status, created_at"),
        {"t": str(tenant_id), "p": str(project_id),
         "pr": str(principal_id) if principal_id else None,
         "k": kind, "a": json.dumps(arguments or {})}).mappings().one()
    return _shape(row["id"], row["status"], created_at=row["created_at"])


def get(conn: Connection, *, tenant_id: UUID, task_id: UUID) -> dict[str, Any] | None:
    row = conn.execute(
        text("SELECT id, kind, status, result, error, progress, input_request, "
             "       request_state, created_at, updated_at, completed_at "
             "  FROM mem.mcp_tasks WHERE id = :i AND tenant_id = :t"),
        {"i": str(task_id), "t": str(tenant_id)}).mappings().one_or_none()
    if row is None:
        return None
    return _shape(
        row["id"], row["status"], kind=row["kind"], result=row["result"],
        error=row["error"], progress=row["progress"],
        input_request=row["input_request"], request_state=row["request_state"],
        created_at=row["created_at"], updated_at=row["updated_at"],
        completed_at=row["completed_at"])


def finish(conn: Connection, *, task_id: UUID, status: str,
           result: dict[str, Any] | None = None,
           error: str | None = None) -> None:
    if status not in TERMINAL:
        raise ValueError(f"{status} is not a terminal status")
    conn.execute(
        text("UPDATE mem.mcp_tasks "
             "   SET status = :s, result = CAST(:r AS jsonb), error = :e, "
             "       updated_at = now(), completed_at = now() "
             " WHERE id = :i AND status NOT IN ('completed','failed','cancelled')"),
        {"s": status, "r": json.dumps(result) if result is not None else None,
         "e": (error or "")[:2000] or None, "i": str(task_id)})


def cancel(conn: Connection, *, tenant_id: UUID, task_id: UUID) -> dict[str, Any] | None:
    conn.execute(
        text("UPDATE mem.mcp_tasks "
             "   SET status = 'cancelled', updated_at = now(), completed_at = now() "
             " WHERE id = :i AND tenant_id = :t "
             "   AND status NOT IN ('completed','failed','cancelled')"),
        {"i": str(task_id), "t": str(tenant_id)})
    return get(conn, tenant_id=tenant_id, task_id=task_id)


def _shape(task_id: Any, status: str, **extra: Any) -> dict[str, Any]:
    """The extension's wire shape, built in exactly one place.

    Assembled here rather than at each call site because tasks/create,
    tasks/get and tasks/update all return the same object, and three
    hand-written copies is how a client ends up branching on which method it
    called before it can read a status.
    """
    out: dict[str, Any] = {"taskId": str(task_id), "status": status}
    for key, value in extra.items():
        if value in (None, {}, ""):
            continue
        camel = {"input_request": "inputRequests",
                 "request_state": "requestState",
                 "created_at": "createdAt", "updated_at": "updatedAt",
                 "completed_at": "completedAt"}.get(key, key)
        out[camel] = value.isoformat() if hasattr(value, "isoformat") else value
    return out
