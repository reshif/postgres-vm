"""Task handles for the MCP Tasks extension — the API half.

The gateway serves `tasks/*` over MCP and holds no database credentials, so the
handle itself lives here, behind the same boundary as everything else that
touches Postgres.

WORK IS QUEUED, NOT RUN INLINE. Creating a task enqueues a Procrastinate job and
returns immediately. Doing the work in the request would make the handle
pointless: the caller would still be waiting, and the poll loop would exist to
report on something that had already finished or timed out.

The one exception is a task whose queue is unavailable. Rather than returning a
handle that will never move, creation fails — a task stuck at `working` forever
is indistinguishable from a slow one, and the client's only recovery is a timeout
it has to invent.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db, mcp_tasks

log = logging.getLogger("memory.routes_tasks")

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


class CreateTask(BaseModel):
    tenant_id: UUID
    project_id: UUID
    principal_id: UUID | None = None
    kind: str
    arguments: dict[str, Any] = {}


class TaskScope(BaseModel):
    tenant_id: UUID
    project_id: UUID
    principal_id: UUID | None = None


class UpdateTask(TaskScope):
    requestState: str | None = None
    inputResponses: dict[str, Any] = {}


@router.post("")
def create_task(req: CreateTask) -> dict:
    if req.kind not in mcp_tasks.KINDS:
        raise HTTPException(400, f"unknown task kind: {req.kind}")
    with db.scoped(req.tenant_id, req.principal_id or req.tenant_id,
                   req.project_id) as conn:
        task = mcp_tasks.create(
            conn, tenant_id=req.tenant_id, project_id=req.project_id,
            principal_id=req.principal_id, kind=req.kind,
            arguments=req.arguments)

    try:
        _enqueue(task["taskId"], req)
    except Exception as exc:  # noqa: BLE001
        # See the module docstring: a handle nobody will ever advance is worse
        # than a failure, because the client cannot tell the difference between
        # that and slow.
        with db.scoped(req.tenant_id, req.principal_id or req.tenant_id,
                       req.project_id) as conn:
            mcp_tasks.finish(conn, task_id=UUID(task["taskId"]), status="failed",
                             error=f"could not enqueue: {exc}")
        raise HTTPException(503, f"task queue unavailable: {exc}") from exc
    return task


def _enqueue(task_id: str, req: CreateTask) -> None:
    """Defer the job onto the worker's queue.

    `with queue.open()` is not optional and not a tidiness detail: Procrastinate
    refuses to defer through an unopened app, and the API process — unlike the
    worker — never opens one, because it does not run jobs. Deferring is a single
    INSERT, so opening a connection for it and closing it again is the right cost
    for an operation that happens once per long-running task.
    """
    from .worker import app as queue

    with queue.open():
        queue.configure_task(
            name=f"mcp_task_{req.kind}", queue="ingestion",
        ).defer(task_id=task_id, tenant_id=str(req.tenant_id),
                project_id=str(req.project_id), arguments=req.arguments)


@router.get("")
def list_tasks(tenant_id: UUID, project_id: UUID,
               principal_id: UUID | None = None, limit: int = 25) -> dict:
    from sqlalchemy import text

    with db.scoped(tenant_id, principal_id or tenant_id, project_id) as conn:
        rows = conn.execute(text(
            "SELECT id, kind, status, created_at, completed_at "
            "  FROM mem.mcp_tasks "
            " WHERE tenant_id = :t AND project_id = :p "
            " ORDER BY created_at DESC LIMIT :limit"),
            {"t": str(tenant_id), "p": str(project_id),
             "limit": min(limit, 100)}).mappings().all()
    return {"tasks": [
        {"taskId": str(r["id"]), "kind": r["kind"], "status": r["status"],
         "createdAt": r["created_at"].isoformat(),
         "completedAt": r["completed_at"].isoformat() if r["completed_at"] else None}
        for r in rows]}


@router.get("/{task_id}")
def get_task(task_id: UUID, tenant_id: UUID, project_id: UUID,
             principal_id: UUID | None = None) -> dict:
    with db.scoped(tenant_id, principal_id or tenant_id, project_id) as conn:
        task = mcp_tasks.get(conn, tenant_id=tenant_id, task_id=task_id)
    if task is None:
        raise HTTPException(404, "no such task")
    return task


@router.post("/{task_id}/cancel")
def cancel_task(task_id: UUID, req: TaskScope) -> dict:
    with db.scoped(req.tenant_id, req.principal_id or req.tenant_id,
                   req.project_id) as conn:
        task = mcp_tasks.cancel(conn, tenant_id=req.tenant_id, task_id=task_id)
    if task is None:
        raise HTTPException(404, "no such task")
    return task


@router.post("/{task_id}/update")
def update_task(task_id: UUID, req: UpdateTask) -> dict:
    """The client answering an inputRequest the task raised.

    Verified through the same MRTR path as a synchronous confirmation, and for
    the same reason: a token that correlates with the request rather than with
    the operation authorises whatever the caller sends next.
    """
    from . import mrtr

    with db.scoped(req.tenant_id, req.principal_id or req.tenant_id,
                   req.project_id) as conn:
        task = mcp_tasks.get(conn, tenant_id=req.tenant_id, task_id=task_id)
        if task is None:
            raise HTTPException(404, "no such task")
        if task["status"] != "input_required":
            raise HTTPException(
                409, f"task is {task['status']}, not waiting for input")

        ok, why = mrtr.verify(f"task:{task.get('kind')}", task.get("arguments", {}),
                              req.requestState, req.inputResponses)
        if not ok:
            raise HTTPException(400, why)

        from sqlalchemy import text
        conn.execute(text(
            "UPDATE mem.mcp_tasks "
            "   SET status = 'working', input_request = NULL, "
            "       request_state = NULL, updated_at = now() "
            " WHERE id = :i"), {"i": str(task_id)})
        return mcp_tasks.get(conn, tenant_id=req.tenant_id, task_id=task_id)
