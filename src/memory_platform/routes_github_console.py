"""Knowledge Console controls for a project-scoped GitHub PAT.

The endpoint accepts a token once, validates it against the repository already
bound to the project, encrypts it before persistence, and never returns the
secret or its ciphertext.  GitHub App webhook configuration remains deployment
owned and is intentionally outside this browser workflow.
"""
from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import text

from . import db, github_credentials
from .github_client import GitHubApiError, GitHubPatClient
from .config import settings


router = APIRouter(prefix="/v1/console/integrations/github", tags=["console", "github"])


class PatConnectRequest(BaseModel):
    tenant_id: UUID
    project_id: UUID
    principal_id: UUID | None = None
    token: SecretStr = Field(min_length=20, max_length=1024)


def _project(conn, *, tenant_id: UUID, project_id: UUID) -> dict:
    row = conn.execute(text(
        "SELECT source_provider, repo_url, evidence_repo_url, github_installation_id, git_default_branch "
        "FROM mem.projects WHERE tenant_id = :tenant AND id = :project"),
        {"tenant": str(tenant_id), "project": str(project_id)}).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "project is not available in this scope")
    return dict(row)


def _audit(conn, *, tenant_id: UUID, project_id: UUID, principal_id: UUID | None,
           action: str, detail: dict) -> None:
    # Metadata only — a credential must never turn up in the audit log.
    conn.execute(text(
        "INSERT INTO mem.audit_log "
        " (tenant_id, principal_id, action, object_type, object_id, scope_context, outcome, detail) "
        "VALUES (:tenant, :principal, :action, 'github_credential', :project, "
        "        CAST(:scope AS jsonb), 'allow', CAST(:detail AS jsonb))"),
        {"tenant": str(tenant_id), "principal": str(principal_id) if principal_id else None,
         "action": action, "project": str(project_id),
         "scope": json.dumps({"tenant": str(tenant_id), "project": str(project_id)}),
         "detail": json.dumps(detail)},
    )


def _status_payload(project: dict, credential: github_credentials.PatMetadata | None) -> dict:
    pat = None
    if credential:
        pat = {
            "configured": True,
            "token_hint": credential.token_hint,
            "github_login": credential.github_login,
            "scopes": list(credential.scopes),
            "validated_at": credential.validated_at.isoformat(),
            "last_used_at": credential.last_used_at.isoformat() if credential.last_used_at else None,
            "last_error": credential.last_error,
        }
    return {
        "github_project": project["source_provider"] == "github",
        "webhooks_enabled": settings().github_enabled,
        "source_repository": project["repo_url"],
        "evidence_repository": project["evidence_repo_url"],
        "default_branch": project["git_default_branch"],
        "github_app_installed": bool(project["github_installation_id"]),
        "pat": pat,
    }


@router.get("")
def github_connection(
    tenant_id: UUID, project_id: UUID, principal_id: UUID | None = None,
) -> dict:
    """Return connection metadata only; PAT contents and ciphertext stay private."""
    with db.scoped(tenant_id, principal_id or tenant_id, project_id) as conn:
        project = _project(conn, tenant_id=tenant_id, project_id=project_id)
        credential = github_credentials.status(conn, project_id=project_id)
    return _status_payload(project, credential)


@router.put("/pat")
def connect_pat(req: PatConnectRequest) -> dict:
    """Validate and save a fine-grained PAT for this existing GitHub project."""
    if not settings().github_enabled:
        raise HTTPException(409, "enable the deployment GitHub integration before connecting a PAT")
    token = req.token.get_secret_value().strip()
    try:
        # Fail before issuing an external request if token encryption is not
        # configured. The temporary result is immediately discarded.
        github_credentials.protect(token)
    except github_credentials.CredentialError as exc:
        raise HTTPException(503, str(exc)) from exc

    with db.scoped(req.tenant_id, req.principal_id or req.tenant_id, req.project_id) as conn:
        project = _project(conn, tenant_id=req.tenant_id, project_id=req.project_id)
    if project["source_provider"] != "github" or not project["repo_url"]:
        raise HTTPException(422, "bind a GitHub source repository before adding a PAT")

    try:
        with GitHubPatClient(token=token, api_url=settings().github_api_url) as client:
            login, scopes = client.validate_repository(str(project["repo_url"]))
            # Evidence pushes make the worker read both repositories: the
            # sidecar itself and the immutable source blobs it cites. Validate
            # both at connection time instead of discovering a missing grant on
            # the first production webhook.
            if project["evidence_repo_url"]:
                client.validate_repository(str(project["evidence_repo_url"]))
    except GitHubApiError as exc:
        # Do not include a response body: provider messages can reflect private
        # repository metadata and would be persisted by browser diagnostics.
        raise HTTPException(422, f"GitHub could not validate this token: {exc}") from exc

    with db.scoped(req.tenant_id, req.principal_id or req.tenant_id, req.project_id) as conn:
        current = _project(conn, tenant_id=req.tenant_id, project_id=req.project_id)
        if current["repo_url"] != project["repo_url"]:
            raise HTTPException(409, "project repository changed while the token was validated; try again")
        credential = github_credentials.store_pat(
            conn, tenant_id=req.tenant_id, project_id=req.project_id,
            principal_id=req.principal_id, token=token, github_login=login, scopes=scopes)
        _audit(conn, tenant_id=req.tenant_id, project_id=req.project_id,
               principal_id=req.principal_id, action="console.github_pat.connected",
               detail={"token_hint": credential.token_hint, "github_login": login})
    return _status_payload(current, credential)


@router.delete("/pat")
def disconnect_pat(
    tenant_id: UUID, project_id: UUID, principal_id: UUID | None = None,
) -> dict:
    """Delete the encrypted PAT, falling back to the GitHub App when present."""
    with db.scoped(tenant_id, principal_id or tenant_id, project_id) as conn:
        project = _project(conn, tenant_id=tenant_id, project_id=project_id)
        removed = github_credentials.delete_pat(conn, project_id=project_id)
        if removed:
            _audit(conn, tenant_id=tenant_id, project_id=project_id,
                   principal_id=principal_id, action="console.github_pat.disconnected", detail={})
    return {**_status_payload(project, None), "removed": removed}
