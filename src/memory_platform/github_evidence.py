"""Signed GitHub delivery handling and durable evidence persistence.

This module is intentionally smaller than a GitHub SDK integration. Its first
responsibility is a security boundary: validate the provider signature, retain
only a normalized delivery envelope, and keep unreviewed event text out of the
retrieval corpus. Fetching immutable Git objects and projecting reviewed
assertions are later pipeline stages, both built on these durable rows.

The source repository and the evidence repository remain Git authorities.
PostgreSQL here is a rebuildable delivery/evidence ledger and query projection,
not another place for agents to author unchecked project truth.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection


class WebhookError(ValueError):
    """A delivery failed validation before it could enter the ledger."""


SUPPORTED_EVENTS = frozenset({
    "check_run", "deployment_status", "ping", "pull_request", "push",
    "workflow_run",
})

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


@dataclass(frozen=True)
class GitHubDelivery:
    delivery_id: str
    event_name: str
    repository_url: str
    repository_full_name: str
    revision: str | None
    ref: str | None
    occurred_at: datetime
    payload_sha256: str
    metadata: dict[str, Any]


def normalize_repository_url(value: str) -> str:
    """Return a transport-independent repository identity.

    Project binding must treat `git@github.com:org/repo.git` and
    `https://github.com/org/repo` as one repository. The same normalized form
    is used by registration, webhook resolution, and later GitHub API fetches.
    """
    raw = (value or "").strip().lower().rstrip("/")
    if not raw:
        return ""
    if raw.startswith("git@"):
        raw = raw[4:]
        if ":" in raw:
            host, path = raw.split(":", 1)
            raw = f"{host}/{path}"
    elif raw.startswith(("ssh://", "git+ssh://", "http://", "https://")):
        parsed = urlparse(raw.replace("git+ssh://", "ssh://", 1))
        raw = f"{parsed.hostname or ''}{parsed.path}"
    raw = raw.removesuffix(".git").strip("/")
    return raw


def payload_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def verify_signature(*, secret: str, payload: bytes, signature: str | None) -> None:
    """Verify GitHub's SHA-256 HMAC without parsing untrusted JSON first."""
    if not secret:
        raise WebhookError("GitHub webhook verification is not configured")
    if not signature or not signature.startswith("sha256="):
        raise WebhookError("missing GitHub SHA-256 signature")
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise WebhookError("invalid GitHub webhook signature")


def _text(value: Any, *, limit: int = 500) -> str:
    """Bound untrusted metadata before it is retained in the audit ledger."""
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.replace("\x00", "").split())
    return cleaned[:limit]


def _subject(value: Any) -> str:
    """Keep a commit subject only; commit bodies are an injection surface."""
    if not isinstance(value, str):
        return ""
    return _text(value.splitlines()[0] if value.splitlines() else "", limit=200)


def _valid_sha(value: Any) -> str | None:
    candidate = _text(value, limit=64)
    return candidate if _SHA_RE.fullmatch(candidate) else None


def _event_time(document: dict[str, Any]) -> datetime:
    """Use GitHub's event time when present; otherwise record receipt time."""
    for key in ("updated_at", "created_at", "timestamp"):
        value = document.get(key)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def parse_delivery(*, delivery_id: str, event_name: str, payload: bytes) -> GitHubDelivery:
    """Parse a signed webhook into a deliberately non-retrievable envelope."""
    if not delivery_id or len(delivery_id) > 200:
        raise WebhookError("missing or invalid GitHub delivery id")
    if event_name not in SUPPORTED_EVENTS:
        raise WebhookError(f"unsupported GitHub event: {event_name or 'missing'}")
    try:
        document = json.loads(payload)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookError("GitHub delivery is not valid JSON") from exc
    if not isinstance(document, dict):
        raise WebhookError("GitHub delivery JSON must be an object")

    repository = document.get("repository")
    if not isinstance(repository, dict):
        raise WebhookError("GitHub delivery has no repository")
    full_name = _text(repository.get("full_name"), limit=200)
    repository_url = _text(
        repository.get("html_url") or repository.get("clone_url"), limit=500
    )
    if not repository_url and full_name:
        repository_url = f"https://github.com/{full_name}"
    if not full_name or not normalize_repository_url(repository_url):
        raise WebhookError("GitHub delivery has an invalid repository")

    revision: str | None = None
    ref: str | None = None
    metadata: dict[str, Any] = {
        "repository": full_name,
        "private": bool(repository.get("private", True)),
    }

    if event_name == "push":
        revision = _valid_sha(document.get("after"))
        ref = _text(document.get("ref"), limit=300) or None
        head = document.get("head_commit") if isinstance(document.get("head_commit"), dict) else {}
        commits = document.get("commits") if isinstance(document.get("commits"), list) else []
        metadata["push"] = {
            "before": _valid_sha(document.get("before")),
            "after": revision,
            "ref": ref,
            "created": bool(document.get("created")),
            "deleted": bool(document.get("deleted")),
            "forced": bool(document.get("forced")),
            "commit_count": min(len(commits), 10_000),
            "head_subject": _subject(head.get("message")),
            "author": _text((head.get("author") or {}).get("username"), limit=200)
            if isinstance(head.get("author"), dict) else "",
        }
    elif event_name == "workflow_run":
        run = document.get("workflow_run") if isinstance(document.get("workflow_run"), dict) else {}
        revision = _valid_sha(run.get("head_sha"))
        ref = _text(run.get("head_branch"), limit=300) or None
        metadata["workflow_run"] = {
            "id": run.get("id") if isinstance(run.get("id"), int) else None,
            "name": _text(run.get("name"), limit=200),
            "status": _text(run.get("status"), limit=80),
            "conclusion": _text(run.get("conclusion"), limit=80),
            "head_branch": ref,
            "head_sha": revision,
            "url": _text(run.get("html_url"), limit=500),
        }
    elif event_name == "pull_request":
        pull = document.get("pull_request") if isinstance(document.get("pull_request"), dict) else {}
        revision = _valid_sha(pull.get("merge_commit_sha")) or _valid_sha(
            (pull.get("head") or {}).get("sha") if isinstance(pull.get("head"), dict) else None
        )
        base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
        ref = _text(base.get("ref"), limit=300) or None
        metadata["pull_request"] = {
            "number": document.get("number") if isinstance(document.get("number"), int) else None,
            "action": _text(document.get("action"), limit=80),
            "merged": bool(pull.get("merged")),
            "base_ref": ref,
            "head_sha": _valid_sha((pull.get("head") or {}).get("sha"))
            if isinstance(pull.get("head"), dict) else None,
            "merge_commit_sha": _valid_sha(pull.get("merge_commit_sha")),
            "url": _text(pull.get("html_url"), limit=500),
        }
    elif event_name == "check_run":
        check = document.get("check_run") if isinstance(document.get("check_run"), dict) else {}
        revision = _valid_sha(check.get("head_sha"))
        metadata["check_run"] = {
            "id": check.get("id") if isinstance(check.get("id"), int) else None,
            "name": _text(check.get("name"), limit=200),
            "status": _text(check.get("status"), limit=80),
            "conclusion": _text(check.get("conclusion"), limit=80),
            "head_sha": revision,
            "url": _text(check.get("html_url"), limit=500),
        }
    elif event_name == "deployment_status":
        deployment = document.get("deployment") if isinstance(document.get("deployment"), dict) else {}
        status = document.get("deployment_status") if isinstance(document.get("deployment_status"), dict) else {}
        revision = _valid_sha(deployment.get("sha"))
        ref = _text(deployment.get("ref"), limit=300) or None
        metadata["deployment_status"] = {
            "deployment_id": deployment.get("id") if isinstance(deployment.get("id"), int) else None,
            "environment": _text(deployment.get("environment"), limit=200),
            "ref": ref,
            "sha": revision,
            "state": _text(status.get("state"), limit=80),
            "environment_url": _text(status.get("environment_url"), limit=500),
        }

    return GitHubDelivery(
        delivery_id=delivery_id,
        event_name=event_name,
        repository_url=repository_url,
        repository_full_name=full_name,
        revision=revision,
        ref=ref,
        occurred_at=_event_time(document),
        payload_sha256=payload_sha256(payload),
        metadata=metadata,
    )


def find_project_for_repository(conn: Connection, repository_url: str) -> dict[str, Any] | None:
    """Resolve one project through the registry without exposing the registry."""
    target = normalize_repository_url(repository_url)
    rows = conn.execute(text(
        "SELECT id, tenant_id, repo_url, evidence_repo_url, source_provider "
        "FROM mem.projects WHERE repo_url IS NOT NULL"
    )).mappings().all()
    matches: list[dict[str, Any]] = []
    for row in rows:
        if normalize_repository_url(str(row["repo_url"])) == target:
            matches.append(dict(row) | {"repository_role": "source"})
        if (row["evidence_repo_url"] and
                normalize_repository_url(str(row["evidence_repo_url"])) == target):
            matches.append(dict(row) | {"repository_role": "evidence"})
    return dict(matches[0]) if len(matches) == 1 else None


def record_delivery(
    conn: Connection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    delivery: GitHubDelivery,
    repository_role: str = "source",
) -> dict[str, Any]:
    """Persist one signed envelope. GitHub retries become a harmless no-op."""
    if repository_role not in {"source", "evidence"}:
        raise ValueError(f"unknown GitHub repository role: {repository_role}")
    row = conn.execute(text(
        "INSERT INTO mem.github_deliveries "
        "(tenant_id, project_id, provider, delivery_id, event_name, repository_url, "
        " repository_full_name, repository_role, revision, ref, occurred_at, payload_sha256, metadata) "
        "VALUES (:tenant, :project, 'github', :delivery, :event, :repository, "
        "        :full_name, :role, :revision, :ref, :occurred, :digest, CAST(:metadata AS jsonb)) "
        "ON CONFLICT (provider, delivery_id) DO NOTHING "
        "RETURNING id, status, received_at"
    ), {
        "tenant": str(tenant_id), "project": str(project_id),
        "delivery": delivery.delivery_id, "event": delivery.event_name,
        "repository": delivery.repository_url,
        "full_name": delivery.repository_full_name, "role": repository_role,
        "revision": delivery.revision,
        "ref": delivery.ref, "occurred": delivery.occurred_at,
        "digest": delivery.payload_sha256, "metadata": json.dumps(delivery.metadata),
    }).mappings().one_or_none()
    if row is None:
        return {"created": False, "delivery_id": delivery.delivery_id}
    return {"created": True, "id": str(row["id"]), "status": row["status"],
            "received_at": row["received_at"]}


def get_delivery(conn: Connection, *, delivery_id: UUID) -> dict[str, Any] | None:
    row = conn.execute(text(
        "SELECT id, tenant_id, project_id, delivery_id, event_name, repository_url, "
        "       repository_full_name, repository_role, revision, ref, occurred_at, "
        "       payload_sha256, metadata, status "
        "  FROM mem.github_deliveries WHERE id = :id"
    ), {"id": str(delivery_id)}).mappings().one_or_none()
    return dict(row) if row else None


def update_delivery(
    conn: Connection, *, delivery_id: UUID, status: str, error: str | None = None
) -> None:
    if status not in {"queued", "processed", "ignored", "failed"}:
        raise ValueError(f"invalid delivery state: {status}")
    conn.execute(text(
        "UPDATE mem.github_deliveries "
        "   SET status = :status, error = :error, processed_at = "
        "       CASE WHEN :status IN ('processed', 'ignored', 'failed') THEN now() ELSE NULL END "
        " WHERE id = :id"
    ), {"id": str(delivery_id), "status": status,
        "error": (error or "")[:1000] or None})


def record_event_artifact(conn: Connection, *, delivery: dict[str, Any]) -> dict[str, Any]:
    """Create the immutable evidence record associated with one delivery.

    This artifact is provenance, not a retrieval document. It has no free-form
    PR body, review body, or workflow log. Later extractors fetch exact Git
    blobs by revision and attach them as distinct artifacts.
    """
    kinds = {
        "push": "git_commit", "pull_request": "pull_request",
        "workflow_run": "ci_run", "check_run": "ci_run",
        "deployment_status": "deployment",
    }
    kind = kinds.get(str(delivery["event_name"]), "git_event")
    row = conn.execute(text(
        "INSERT INTO mem.evidence_artifacts "
        "(tenant_id, project_id, provider, kind, external_id, source_repository, "
        " source_revision, source_ref, location, content_sha256, observed_at, metadata) "
        "VALUES (:tenant, :project, 'github', :kind, :external, :repository, "
        "        :revision, :ref, :location, :digest, :observed, CAST(:metadata AS jsonb)) "
        "ON CONFLICT (project_id, provider, kind, external_id) DO NOTHING "
        "RETURNING id"
    ), {
        "tenant": str(delivery["tenant_id"]), "project": str(delivery["project_id"]),
        "kind": kind, "external": str(delivery["delivery_id"]),
        "repository": delivery["repository_url"], "revision": delivery["revision"],
        "ref": delivery["ref"], "location": delivery["repository_url"],
        "digest": delivery["payload_sha256"], "observed": delivery["occurred_at"],
        "metadata": json.dumps(delivery["metadata"]),
    }).mappings().one_or_none()
    if row is None:
        return {"created": False}
    return {"created": True, "id": str(row["id"]), "kind": kind}


def public_delivery(delivery: GitHubDelivery) -> dict[str, Any]:
    """Small safe response shape for the webhook endpoint and test fixtures."""
    value = asdict(delivery)
    value["occurred_at"] = delivery.occurred_at.isoformat()
    return value
