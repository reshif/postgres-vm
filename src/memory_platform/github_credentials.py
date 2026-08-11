"""Project-scoped storage for GitHub fine-grained PATs.

Only ciphertext crosses the database boundary.  The credential endpoint accepts
the token once and the status endpoint intentionally exposes only safe metadata.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from typing import Sequence
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text
from sqlalchemy.engine import Connection

from .config import settings


class CredentialError(ValueError):
    """A credential cannot safely be stored or used."""


@dataclass(frozen=True)
class PatMetadata:
    token_hint: str
    token_fingerprint: str
    github_login: str | None
    scopes: tuple[str, ...]
    validated_at: datetime
    last_used_at: datetime | None
    last_error: str | None


def _vault() -> Fernet:
    key = settings().github_pat_encryption_key.strip()
    if not key:
        raise CredentialError(
            "GitHub PAT storage is not configured; set MEMORY_GITHUB_PAT_ENCRYPTION_KEY")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise CredentialError("MEMORY_GITHUB_PAT_ENCRYPTION_KEY is not a valid Fernet key") from exc


def protect(token: str) -> tuple[bytes, str, str]:
    """Encrypt a token and return only non-secret display/audit metadata."""
    value = token.strip()
    if len(value) < 20:
        raise CredentialError("GitHub token is too short")
    return (
        _vault().encrypt(value.encode("utf-8")),
        f"****{value[-4:]}",
        hashlib.sha256(value.encode("utf-8")).hexdigest(),
    )


def reveal(ciphertext: bytes) -> str:
    try:
        return _vault().decrypt(ciphertext).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise CredentialError("stored GitHub credential cannot be decrypted") from exc


def store_pat(
    conn: Connection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    principal_id: UUID | None,
    token: str,
    github_login: str | None,
    scopes: Sequence[str],
) -> PatMetadata:
    ciphertext, hint, fingerprint = protect(token)
    cleaned_scopes = tuple(sorted({scope for scope in scopes if scope}))
    row = conn.execute(text(
        "INSERT INTO mem.github_pat_credentials "
        " (tenant_id, project_id, token_ciphertext, token_hint, token_fingerprint, "
        "  github_login, scopes, created_by) "
        "VALUES (:tenant, :project, :ciphertext, :hint, :fingerprint, :login, :scopes, :principal) "
        "ON CONFLICT (project_id, provider) DO UPDATE SET "
        " token_ciphertext = EXCLUDED.token_ciphertext, token_hint = EXCLUDED.token_hint, "
        " token_fingerprint = EXCLUDED.token_fingerprint, github_login = EXCLUDED.github_login, "
        " scopes = EXCLUDED.scopes, validated_at = now(), last_error = NULL, "
        " created_by = EXCLUDED.created_by, updated_at = now() "
        "RETURNING token_hint, token_fingerprint, github_login, scopes, validated_at, last_used_at, last_error"),
        {"tenant": str(tenant_id), "project": str(project_id), "ciphertext": ciphertext,
         "hint": hint, "fingerprint": fingerprint, "login": github_login,
         "scopes": list(cleaned_scopes), "principal": str(principal_id) if principal_id else None},
    ).mappings().one()
    return _metadata(row)


def load_pat(conn: Connection, *, project_id: UUID) -> str | None:
    ciphertext = conn.execute(text(
        "SELECT token_ciphertext FROM mem.github_pat_credentials "
        "WHERE project_id = :project AND provider = 'github_pat'"),
        {"project": str(project_id)}).scalar_one_or_none()
    return reveal(bytes(ciphertext)) if ciphertext is not None else None


def status(conn: Connection, *, project_id: UUID) -> PatMetadata | None:
    row = conn.execute(text(
        "SELECT token_hint, token_fingerprint, github_login, scopes, validated_at, last_used_at, last_error "
        "FROM mem.github_pat_credentials WHERE project_id = :project AND provider = 'github_pat'"),
        {"project": str(project_id)}).mappings().one_or_none()
    return _metadata(row) if row else None


def record_use(conn: Connection, *, project_id: UUID, error: str | None = None) -> None:
    # Errors are deliberately bounded and never contain an HTTP body or token.
    safe_error = (error or "").replace("\n", " ")[:300] or None
    conn.execute(text(
        "UPDATE mem.github_pat_credentials SET last_used_at = now(), last_error = :error, "
        "updated_at = now() WHERE project_id = :project AND provider = 'github_pat'"),
        {"project": str(project_id), "error": safe_error})


def delete_pat(conn: Connection, *, project_id: UUID) -> bool:
    return conn.execute(text(
        "DELETE FROM mem.github_pat_credentials WHERE project_id = :project "
        "AND provider = 'github_pat'"), {"project": str(project_id)}).rowcount > 0


def _metadata(row) -> PatMetadata:
    return PatMetadata(
        token_hint=str(row["token_hint"]), token_fingerprint=str(row["token_fingerprint"]),
        github_login=str(row["github_login"]) if row["github_login"] else None,
        scopes=tuple(row["scopes"] or ()), validated_at=row["validated_at"],
        last_used_at=row["last_used_at"], last_error=row["last_error"],
    )
