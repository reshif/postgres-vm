"""OAuth token verification and scope resolution (ADR-0004).

ADR-0004: "Memory identity, authorization and project binding never derive from
client-supplied IDs." Until now the MCP gateway bound one project from an
environment variable, which was a documented development affordance. This is the
real thing.

WHAT THE TOKEN CARRIES AND WHAT IT DOES NOT. The token names an organisation and
a project by SLUG, plus a subject. It never carries database UUIDs. The server
resolves slugs to ids against the registry, so a forged or altered claim names a
project that either does not exist (rejected) or exists and is exactly the one
the operator granted — it cannot conjure access to a row by guessing an id.

FAILURES ARE LOUD AND SPECIFIC, BUT NEVER ORACLES. An invalid token says which
check failed (expired, wrong audience, unknown key) because an operator debugging
integration needs that. It never says whether a tenant or project EXISTS: an
unknown project and a project you may not see return the same error, or the 401
becomes a directory of other people's projects.

ALGORITHM IS PINNED. The allowed algorithms are configured, never read from the
token header. Trusting the header is the classic JWT break: `alg: none` accepts
an unsigned token, and `alg: HS256` against an RSA public key lets anyone who can
read the public JWKS forge a signature with it. Both are tested in
tests/test_auth.py.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .config import settings

log = logging.getLogger("memory.auth")


class AuthError(Exception):
    """Token rejected. Message is safe to return to the caller (401)."""


class Forbidden(Exception):
    """Token is valid but does not grant this scope (403)."""


@dataclass(frozen=True)
class Scope:
    """A resolved, server-side scope. The only thing handlers should trust."""
    tenant_id: UUID
    project_id: UUID
    principal_id: UUID
    org_slug: str
    project_slug: str
    subject: str

    def as_params(self) -> dict[str, str]:
        return {"tenant_id": str(self.tenant_id), "project_id": str(self.project_id),
                "principal_id": str(self.principal_id)}


# ------------------------------------------------------------------ JWKS cache
class _JWKS:
    """Cached JWKS with a bounded refresh rate.

    Refreshed on an unknown `kid` so key rotation works without a restart, but at
    most once per `min_refresh_s` — otherwise a token carrying a random kid turns
    every request into an outbound HTTP call, which is a denial of service
    against the identity provider using our own auth path.
    """

    def __init__(self, url: str, ttl_s: int = 3600, min_refresh_s: int = 30) -> None:
        self.url = url
        self.ttl_s = ttl_s
        self.min_refresh_s = min_refresh_s
        self._keys: dict[str, Any] = {}
        self._fetched = 0.0
        self._last_attempt = 0.0
        self._lock = threading.Lock()

    def _fetch(self) -> None:
        try:
            with urllib.request.urlopen(self.url, timeout=10) as r:
                doc = json.load(r)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise AuthError(f"cannot reach the JWKS endpoint: {exc}") from exc

        import jwt  # local: keeps import cost off the hot path when auth is off

        keys = {}
        for k in doc.get("keys", []):
            kid = k.get("kid")
            if not kid:
                continue
            try:
                keys[kid] = jwt.PyJWK(k).key
            except Exception as exc:  # noqa: BLE001
                log.warning("skipping unusable JWKS key %s: %s", kid, exc)
        if not keys:
            raise AuthError("JWKS document contained no usable keys")
        self._keys = keys
        self._fetched = time.monotonic()

    def key_for(self, kid: str | None):
        now = time.monotonic()
        with self._lock:
            stale = now - self._fetched > self.ttl_s
            if not self._keys or stale:
                self._fetch()
            if kid and kid not in self._keys:
                if now - self._last_attempt > self.min_refresh_s:
                    self._last_attempt = now
                    self._fetch()
        key = self._keys.get(kid) if kid else next(iter(self._keys.values()), None)
        if key is None:
            raise AuthError("token signed with an unknown key")
        return key


def discover_jwks_url(issuer: str) -> str:
    """Resolve the JWKS endpoint from the issuer's OIDC discovery document.

    Every OpenID provider publishes `jwks_uri` at
    `{issuer}/.well-known/openid-configuration`, and every OIDC client reads it
    from there. Requiring an operator to copy that URL into a second setting adds
    a step whose only possible outcomes are "same as discovery" and "wrong" — and
    a JWKS URL pointing somewhere other than the issuer's own is precisely the
    misconfiguration that makes signature verification meaningless.

    This was found by pointing the stack at a real Keycloak: every request failed
    with `MEMORY_OAUTH_JWKS_URL is not configured`. tests/test_auth.py could not
    have caught it — it injects a verification key directly and never resolves an
    endpoint at all, which is why a synthetic-key suite is not a substitute for
    one real provider.

    `oauth_jwks_url` remains supported and still wins when set, for providers with
    a non-standard discovery path or a network where it is not reachable.
    """
    if not issuer:
        return ""
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            doc = json.load(r)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise AuthError(f"OIDC discovery failed at {url}: {exc}") from exc

    jwks_uri = str(doc.get("jwks_uri") or "")
    # The discovered issuer must match the one we were configured with, or the
    # document is describing a different provider and its keys prove nothing
    # about our tokens (RFC 8414 requires this check).
    discovered_issuer = str(doc.get("issuer") or "")
    if discovered_issuer and discovered_issuer.rstrip("/") != issuer.rstrip("/"):
        raise AuthError(
            f"OIDC discovery at {url} declares issuer {discovered_issuer!r}, "
            f"which is not the configured issuer {issuer!r}")
    if jwks_uri:
        log.info("discovered JWKS endpoint %s for issuer %s", jwks_uri, issuer)
    return jwks_uri


_jwks: _JWKS | None = None
_test_key: Any = None      # set only by tests; see verify_token


def configure_test_key(key: Any) -> None:
    """Inject a verification key, bypassing JWKS fetch. Tests only.

    Exists so the token-validation logic can be tested offline and exhaustively.
    Every other property — expiry, audience, issuer, algorithm pinning — is
    exercised through exactly the same code path production uses.
    """
    global _test_key
    _test_key = key


def enabled() -> bool:
    return bool(settings().oauth_issuer)


def verify_token(token: str) -> dict[str, Any]:
    """Verify signature and standard claims. Returns the claim set."""
    import jwt

    s = settings()
    if not s.oauth_issuer:
        raise AuthError("OAuth is not configured on this server")

    try:
        header = jwt.get_unverified_header(token)
    except Exception as exc:  # noqa: BLE001
        raise AuthError(f"malformed token: {exc}") from exc

    if _test_key is not None:
        key = _test_key
    else:
        global _jwks
        if _jwks is None:
            jwks_url = s.oauth_jwks_url or discover_jwks_url(s.oauth_issuer)
            if not jwks_url:
                raise AuthError(
                    "cannot determine the JWKS endpoint: MEMORY_OAUTH_JWKS_URL is "
                    f"unset and OIDC discovery against {s.oauth_issuer} did not "
                    "return a jwks_uri")
            _jwks = _JWKS(jwks_url)
        key = _jwks.key_for(header.get("kid"))

    try:
        return jwt.decode(
            token,
            key=key,
            # Pinned. NEVER header["alg"] — that is how `alg: none` and
            # HS256-signed-with-the-public-key forgeries get accepted.
            algorithms=[a.strip() for a in s.oauth_algorithms.split(",") if a.strip()],
            audience=s.oauth_audience or None,
            issuer=s.oauth_issuer,
            options={"require": ["exp", "iss", "sub"]},
            leeway=s.oauth_leeway_s,
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token has expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise AuthError("token audience does not match this server") from exc
    except jwt.InvalidIssuerError as exc:
        raise AuthError("token issuer is not trusted") from exc
    except jwt.MissingRequiredClaimError as exc:
        raise AuthError(f"token is missing a required claim: {exc.claim}") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError(f"token rejected: {exc}") from exc


def audit_denied(conn: Connection, *, reason: str, claims: dict[str, Any] | None = None,
                 tenant_id: Any = None) -> None:
    """Record a refused scope resolution.

    Suite 2's first case is "Project A memory, query from project B — not
    returned, AND `scope.denied` audited". The first half is RLS's job and it
    does it silently, which is the point: a policy that errors tells an attacker
    which projects exist. The consequence is that refusal leaves NO trace
    anywhere unless something writes one deliberately, and "we were probed for a
    week" would be unanswerable.

    Deliberately never raises. This runs on a path that is already failing the
    request, and an audit-write error must not turn a clean 403 into a 500 —
    that would make the audit trail a denial-of-service surface.

    Writes are best-effort about identity too: a rejected token may name a tenant
    that does not exist, so tenant_id is nullable here and the claim values are
    kept in `detail` for correlation.
    """
    from . import metrics as _metrics

    _metrics.denied(reason)
    try:
        # SAVEPOINT, not just try/except. Scope resolution runs BEFORE any scope
        # exists, so this INSERT can be refused by RLS — and a refused statement
        # marks the whole transaction aborted, after which every later statement
        # fails with "current transaction is aborted". Catching the Python
        # exception does not un-poison the transaction; only a nested rollback
        # does. Without this the audit call turned a clean 403 into a cascade of
        # failures in the caller.
        with conn.begin_nested():
            conn.execute(
                text("INSERT INTO mem.audit_log "
                 "  (tenant_id, principal_id, action, object_type, object_id, "
                 "   scope_context, outcome, detail) "
                 "VALUES (:t, NULL, 'scope.denied', 'scope', NULL, "
                 "        CAST(:sc AS jsonb), 'deny', CAST(:d AS jsonb))"),
                {"t": str(tenant_id) if tenant_id else None,
                 "sc": json.dumps({"claimed": {
                     k: str(v)[:100] for k, v in (claims or {}).items()
                     if k in ("sub", "iss", "aud", settings().oauth_org_claim,
                              settings().oauth_project_claim)}}),
                 "d": json.dumps({"reason": reason[:300]})},
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("scope denial could not be audited: %s", exc)


def resolve_scope(conn: Connection, claims: dict[str, Any]) -> Scope:
    """Map verified claims to a server-side scope.

    Slugs in, UUIDs out. The token never names a database id, so a tampered
    claim can only ever point at a project the operator actually created.
    """
    s = settings()
    org_slug = str(claims.get(s.oauth_org_claim) or "").strip()
    project_slug = str(claims.get(s.oauth_project_claim) or "").strip()
    subject = str(claims.get("sub") or "").strip()

    if not org_slug or not project_slug:
        audit_denied(conn, reason="token carries no project binding", claims=claims)
        raise Forbidden(
            f"token carries no project binding (expected `{s.oauth_org_claim}` and "
            f"`{s.oauth_project_claim}` claims)")

    row = conn.execute(
        text("SELECT p.id AS project_id, o.id AS tenant_id "
             "  FROM mem.projects p JOIN mem.organizations o ON o.id = p.tenant_id "
             " WHERE o.slug = :o AND p.slug = :p"),
        {"o": org_slug, "p": project_slug},
    ).mappings().one_or_none()
    if row is None:
        # Deliberately identical to the "not granted" case: distinguishing them
        # turns 403 into a directory of every project on the server.
        audit_denied(conn, reason="token names an unknown org/project", claims=claims)
        raise Forbidden("token does not grant access to a known project")

    # Principals are provisioned on first sight, scoped to the tenant the token
    # named. `sub` is the identity provider's stable subject, so the same user
    # in two tenants is two principals — which is correct: they are two
    # identities that happen to share a login.
    principal = conn.execute(
        text("INSERT INTO mem.principals "
             "  (id, tenant_id, actor, external_id, display_name) "
             "VALUES (:i, :t, 'human', :e, :d) "
             "ON CONFLICT (tenant_id, actor, external_id) DO UPDATE "
             "  SET display_name = EXCLUDED.display_name "
             "RETURNING id"),
        {"i": str(uuid4()), "t": str(row["tenant_id"]), "e": subject,
         "d": str(claims.get("name") or subject)[:200]},
    ).scalar_one()

    return Scope(tenant_id=row["tenant_id"], project_id=row["project_id"],
                 principal_id=principal, org_slug=org_slug,
                 project_slug=project_slug, subject=subject)


def bearer(header_value: str | None) -> str:
    """Extract a bearer token from an Authorization header."""
    if not header_value:
        raise AuthError("missing Authorization header")
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthError("Authorization header must be `Bearer <token>`")
    return parts[1].strip()
