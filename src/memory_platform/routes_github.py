"""GitHub App webhook ingress for the Git-native evidence pipeline."""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from . import db, github_evidence
from .config import settings

log = logging.getLogger("memory.routes_github")
router = APIRouter(prefix="/v1/integrations/github", tags=["github"])


@router.post("/webhooks", status_code=202)
async def receive_webhook(request: Request) -> dict:
    """Accept a signed event and enqueue it without treating its text as memory."""
    cfg = settings()
    if not cfg.github_enabled:
        # A disabled integration has no externally discoverable operational
        # surface. It is not a failed delivery and GitHub will not retry it.
        raise HTTPException(404, "GitHub integration is disabled")

    declared_size = request.headers.get("content-length")
    if declared_size and declared_size.isdigit() and int(declared_size) > cfg.github_webhook_max_bytes:
        raise HTTPException(413, "GitHub delivery is too large")
    payload = await request.body()
    if len(payload) > cfg.github_webhook_max_bytes:
        raise HTTPException(413, "GitHub delivery is too large")

    try:
        github_evidence.verify_signature(
            secret=cfg.github_webhook_secret,
            payload=payload,
            signature=request.headers.get("x-hub-signature-256"),
        )
        event_name = request.headers.get("x-github-event", "")
        delivery = github_evidence.parse_delivery(
            delivery_id=request.headers.get("x-github-delivery", ""),
            event_name=event_name,
            payload=payload,
        )
    except github_evidence.WebhookError as exc:
        raise HTTPException(401, str(exc)) from exc

    # A successful GitHub ping has no project work to schedule. It still proves
    # the signature setup without creating an audit row outside a project scope.
    if delivery.event_name == "ping":
        return {"accepted": True, "queued": False, "event": "ping"}

    with db.engine().connect() as conn:
        project = github_evidence.find_project_for_repository(conn, delivery.repository_url)
    if project is None:
        # Do not reveal whether a repository is registered. An App may be
        # installed at organization scope and receive events for other repos.
        log.info("ignoring GitHub delivery for unbound or ambiguous repository")
        return {"accepted": True, "queued": False}
    if project["source_provider"] != "github":
        log.info("ignoring GitHub delivery for a legacy project binding")
        return {"accepted": True, "queued": False}

    tenant_id = UUID(str(project["tenant_id"]))
    project_id = UUID(str(project["id"]))
    with db.scoped(tenant_id, tenant_id, project_id) as conn:
        persisted = github_evidence.record_delivery(
            conn, tenant_id=tenant_id, project_id=project_id, delivery=delivery,
            repository_role=str(project["repository_role"]))
        if not persisted["created"]:
            return {"accepted": True, "queued": False, "duplicate": True}
        delivery_id = UUID(str(persisted["id"]))
        github_evidence.update_delivery(conn, delivery_id=delivery_id, status="queued")

    try:
        _enqueue(delivery_id, project_id)
    except Exception as exc:  # noqa: BLE001
        with db.scoped(tenant_id, tenant_id, project_id) as conn:
            github_evidence.update_delivery(
                conn, delivery_id=delivery_id, status="failed", error=f"queue unavailable: {exc}")
        # 503 tells GitHub to retry the same signed delivery. We never claim an
        # accepted event has work scheduled when it does not.
        raise HTTPException(503, "GitHub delivery queue is unavailable") from exc
    return {"accepted": True, "queued": True, "delivery_id": str(delivery_id)}


def _enqueue(delivery_id: UUID, project_id: UUID) -> None:
    """Schedule deterministic processing using the same durable queue as MCP tasks."""
    from .worker import app as queue

    with queue.open():
        queue.configure_task(name="process_github_delivery", queue="github").defer(
            delivery_id=str(delivery_id), project_id=str(project_id))
