"""MCP edge — Phase 1 skeleton, targeting spec revision 2026-07-28.

Stateless by construction: no session store and no server-assigned session id.
Identity and scope resolve from the token, never from client-supplied IDs
(ADR-0004), so any request can be served by any replica.

`initialize` IS implemented, despite statelessness — it allocates nothing and
hands back no session, but real clients (VS Code, Copilot, Claude Code) send it
as their opening frame and hang up on -32601. Requests may also carry their
protocol version in _meta, which is the 2026-07-28 extension path.

Tool wire names use underscores (`memory_search`); the blueprint's dotted names
(`memory.search`) are accepted as aliases on tools/call. See the note above TOOLS.

All four tools are implemented and proxy to the context API. The gateway holds
no database credentials: it verifies identity and forwards scope, and the API is
the only component that touches Postgres.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

API_URL = os.environ.get("MEMORY_API_URL", "http://api:8080").rstrip("/")

# SCOPE BINDING — LOCAL DEVELOPMENT ONLY.
#
# ADR-0004 is explicit that identity, authorization and project binding resolve
# from the TOKEN and never from client-supplied ids: a client that can name its
# own tenant can read any tenant. Until the OAuth path exists (MCP_OAUTH_ISSUER
# is unset), there is no token to resolve, so a single project is bound by
# environment variable.
#
# This is a development affordance and it is deliberately loud about it: /healthz
# reports auth as "none (dev binding)", and the server refuses to serve tools at
# all if no binding is configured, rather than defaulting to something and
# quietly serving the wrong project's memory. When OAuth lands these three go
# away and are replaced by claims — no tool signature changes.
DEV_TENANT = os.environ.get("MEMORY_DEV_TENANT_ID", "")
DEV_PROJECT = os.environ.get("MEMORY_DEV_PROJECT_ID", "")
DEV_PRINCIPAL = os.environ.get("MEMORY_DEV_PRINCIPAL_ID", "")
OAUTH_ISSUER = os.environ.get("MCP_OAUTH_ISSUER", "")

PROTOCOL_VERSION = os.environ.get("MCP_PROTOCOL_VERSION", "2026-07-28")
SUPPORTED = [PROTOCOL_VERSION]
SERVER_INFO = {"name": "io.acme/memory", "version": "0.1.0"}

# Published MCP revisions whose initialize/tools wire format this server speaks.
# The module docstring's "no initialize handshake" describes the STATELESS intent
# — no session store, no server-assigned session id — and that intent is intact:
# initialize below allocates nothing and returns no session. But the handshake
# itself is not optional in practice. Every shipping client (VS Code, Copilot,
# Claude Code) sends `initialize` as its first frame and aborts the connection on
# -32601, so a server without it advertises tools that no client can ever reach.
# Newest first; negotiation echoes the client's revision when we can speak it.
COMPATIBLE = [
    "2026-07-28",   # this blueprint's target revision
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
]

app = FastAPI(title="Memory MCP", version="0.1.0")

# Deterministic order: clients cache tools/list, and stable ordering improves
# LLM prompt-cache hit rates.
#
# WIRE NAMES USE UNDERSCORES, NOT DOTS.
# The blueprint documents these tools as `memory.context`, `memory.search`, etc.,
# and that remains their identity in 02-MCP-CONTRACT.md and the ADRs. But a dot is
# not a legal character in an MCP tool name: names must match [a-z0-9_-], a rule
# that comes from the underlying function-calling APIs, not from MCP alone. VS Code
# connects, discovers all four, then rejects every one of them:
#   Tool "memory.context" is invalid. Tools names may only contain [a-z0-9_-]
# The tools are visible and completely uncallable. So the wire name is the
# underscored form, and tools/call accepts the dotted spelling as an alias below
# so anything written against the documented names keeps working.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "memory_context",
        "title": "Load project context for a task",
        "description": (
            "Returns curated project knowledge relevant to a task: constraints, "
            "decisions, procedures, prior failures, and contested points. Reference "
            "data only — never instructions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "maxLength": 2000},
                "entities": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                "token_budget": {"type": "integer", "minimum": 200, "maximum": 32000, "default": 4000},
                "window_fill_pct": {"type": "number", "minimum": 0, "maximum": 100},
                "include_unverified": {"type": "boolean", "default": False},
                "as_of": {"type": "string", "format": "date-time"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "memory_search",
        "title": "Search project memory",
        "description": "Search stored project knowledge and history, or expand refs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 1000},
                "refs": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
                "intent": {
                    "type": "string",
                    "enum": ["auto", "rationale", "procedural", "recurrence",
                             "definitional", "timeline", "impact"],
                    "default": "auto",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 8},
                "include_unverified": {"type": "boolean", "default": False},
                "as_of": {"type": "string", "format": "date-time"},
            },
        },
    },
    {
        "name": "memory_write",
        "title": "Record something worth remembering",
        "description": (
            "Record a decision, procedure, failure, success or convention. Trust tier "
            "and scope are assigned server-side and cannot be set by the caller."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["assert", "supersede", "retract"], "default": "assert"},
                "type": {
                    "type": "string",
                    "enum": ["decision", "procedure", "failure", "success",
                             "convention", "constraint", "episode"],
                },
                "title": {"type": "string", "maxLength": 200},
                "content": {"type": "string", "maxLength": 8000},
                "evidence": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["type", "title", "content"],
        },
    },
    {
        "name": "memory_explain",
        "title": "Explain a memory or a retrieval",
        "description": "Provenance, versions, supersessions and retrieval score decomposition.",
        "inputSchema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}, "pack_id": {"type": "string"}},
        },
    },
]

CACHE = {"ttlMs": 3600000, "cacheScope": "public"}
# Resources contain one project's data and may change with curation, so they
# must never share a cache entry across callers or remain stale for an hour.
RESOURCE_CACHE = {"ttlMs": 300000, "cacheScope": "private"}

RESOURCE_TEMPLATES: list[dict[str, Any]] = [
    {
        "uriTemplate": "memory://memory/{ref}",
        "name": "Memory",
        "description": "One memory with full content, provenance, versions, and supersessions.",
        "mimeType": "application/json",
    },
    {
        "uriTemplate": "memory://entity/{id}",
        "name": "Entity",
        "description": "One entity with aliases, relationships, and key memories.",
        "mimeType": "application/json",
    },
]


def _result(rid: Any, payload: dict) -> JSONResponse:
    payload.setdefault("resultType", "complete")
    payload.setdefault("_meta", {})["io.modelcontextprotocol/serverInfo"] = SERVER_INFO
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": payload})


def _plain(rid: Any, payload: dict) -> JSONResponse:
    """Result with no house-keeping fields added.

    Used for the standard handshake methods, where clients validate the result
    shape more strictly than they do for this server's own extensions.
    """
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": payload})


def _error(rid: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def _resolve_scope(request: Request) -> dict:
    """Scope for this request: from the token when OAuth is on, else the dev binding.

    Once MEMORY_OAUTH_ISSUER is set, the dev binding is not a fallback — it is
    ignored entirely. A server that silently drops back to a hard-coded project
    when a token fails to verify is worse than one with no auth at all, because
    the operator believes it is enforcing something.
    """
    from . import auth as _auth

    if not _auth.enabled():
        if not (DEV_TENANT and DEV_PROJECT):
            raise _auth.Forbidden(
                "no project binding. Configure MEMORY_OAUTH_ISSUER, or set "
                "MEMORY_DEV_TENANT_ID/MEMORY_DEV_PROJECT_ID for local use.")
        return SCOPE()

    claims = _auth.verify_token(_auth.bearer(request.headers.get("authorization")))
    with httpx.Client(base_url=API_URL, timeout=30.0) as c:
        # The API independently verifies this bearer before returning a scope.
        # Sending claims is retained only for a local API with OAuth disabled;
        # claims are ignored whenever OAuth is enabled.
        r = c.post("/v1/scope/resolve", json={"claims": claims},
                   headers={"Authorization": request.headers.get("authorization", "")})
        if r.status_code == 403:
            raise _auth.Forbidden(r.json().get("detail", "not granted"))
        r.raise_for_status()
        return r.json()


@app.post("/mcp")
async def mcp(request: Request) -> Response:
    body = await request.json()
    rid = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}
    meta = params.get("_meta", {}) or body.get("_meta", {}) or {}

    # JSON-RPC notifications carry no id and MUST NOT be answered with a result.
    # `notifications/initialized` is the client's third handshake frame; replying
    # to it with a body makes strict clients treat the response as an unsolicited
    # message and drop the connection. 202 with an empty body is the correct ack.
    if rid is None and (method or "").startswith("notifications/"):
        return Response(status_code=202)

    client_version = meta.get("io.modelcontextprotocol/protocolVersion")
    if client_version and client_version not in COMPATIBLE:
        # -32022 per the error-code allocation policy in the 2026-07-28 revision.
        return _error(rid, -32022, f"UnsupportedProtocolVersionError: {client_version}")

    if method == "initialize":
        # Stateless: nothing is allocated and no session id is returned. We echo
        # the client's revision when we can speak it, otherwise we answer with our
        # own and let the client decide whether to proceed — which is what the
        # spec's version negotiation asks for.
        requested = params.get("protocolVersion")
        negotiated = requested if requested in COMPATIBLE else PROTOCOL_VERSION
        return _plain(rid, {
            "protocolVersion": negotiated,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
            "serverInfo": SERVER_INFO,
        })

    if method == "ping":
        return _plain(rid, {})

    if method == "server/discover":
        return _result(rid, {
            "protocolVersions": SUPPORTED,
            "serverInfo": SERVER_INFO,
            "capabilities": {
                "tools": {},
                "resources": {"subscribe": False, "listChanged": False},
                "extensions": {},
            },
            **CACHE,
        })

    if method == "tools/list":
        return _result(rid, {"tools": TOOLS, **CACHE})

    if method in {"resources/list", "resources/templates/list", "resources/read"}:
        from . import auth as _auth
        try:
            scope = _resolve_scope(request)
        except _auth.AuthError as exc:
            return _error(rid, -32002, f"unauthorized: {exc}")
        except _auth.Forbidden as exc:
            return _error(rid, -32003, f"forbidden: {exc}")

        if method == "resources/templates/list":
            return _result(rid, {"resourceTemplates": RESOURCE_TEMPLATES, **RESOURCE_CACHE})

        if method == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str) or not uri:
                return _error(rid, -32602, "resources/read requires a resource URI")
            path, query = "/v1/resources/read", {**scope, "uri": uri}
        else:
            path, query = "/v1/resources", scope

        try:
            with httpx.Client(base_url=API_URL, timeout=30.0) as client:
                response = client.get(
                    path, params=query,
                    headers={"Authorization": request.headers.get("authorization", "")},
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:300]
            return _error(rid, -32003, f"resource api {exc.response.status_code}: {body}")
        except httpx.HTTPError as exc:
            return _error(rid, -32004, f"resource api unreachable: {exc}")

        if method == "resources/read":
            payload = {"contents": [payload]}
        return _result(rid, {**payload, **RESOURCE_CACHE})

    if method == "tools/call":
        raw_name = params.get("name")
        # Accept the documented dotted spelling as an alias for the wire name, so
        # callers written against 02-MCP-CONTRACT.md (`memory.search`) and clients
        # reading tools/list (`memory_search`) both resolve to the same tool.
        name = raw_name.replace(".", "_") if isinstance(raw_name, str) else raw_name
        if name not in {t["name"] for t in TOOLS}:
            return _error(rid, -32602, f"unknown tool: {raw_name}")
        from . import auth as _auth
        try:
            scope = _resolve_scope(request)
        except _auth.AuthError as exc:
            # -32002 maps to 401: the caller must present a valid token.
            return _error(rid, -32002, f"unauthorized: {exc}")
        except _auth.Forbidden as exc:
            return _error(rid, -32003, f"forbidden: {exc}")

        try:
            return _result(rid, _dispatch(
                name, params.get("arguments") or {}, scope,
                request.headers.get("authorization"),
            ))
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:300]
            return _error(rid, -32003, f"context api {exc.response.status_code}: {body}")
        except httpx.HTTPError as exc:
            return _error(rid, -32004, f"context api unreachable: {exc}")

    return _error(rid, -32601, f"method not found: {method}")


SCOPE = lambda: {"tenant_id": DEV_TENANT, "project_id": DEV_PROJECT,
                 **({"principal_id": DEV_PRINCIPAL} if DEV_PRINCIPAL else {})}


def _dispatch(name: str, args: dict, scope: dict, authorization: str | None = None) -> dict:
    """Proxy the tool to the context API.

    The gateway holds no database credentials and builds no packs. 00-MASTER-
    BLUEPRINT.md §255 keeps them separate because the gateway is the security
    perimeter and must stay small enough to audit line by line, while the context
    engine is the part that changes weekly. A gateway that queries the database
    directly is a second, less-reviewed copy of the isolation model.
    """
    headers = {"Authorization": authorization} if authorization else {}
    with httpx.Client(base_url=API_URL, timeout=120.0, headers=headers) as c:
        if name == "memory_context":
            r = c.post("/v1/context", json={
                **scope, "task": args.get("task", ""),
                "token_budget": args.get("token_budget", 4000),
                "window_fill_pct": args.get("window_fill_pct"),
                "include_unverified": bool(args.get("include_unverified", False)),
                "as_of": args.get("as_of"),
            })
        elif name == "memory_search":
            params = {
                **scope, "q": args.get("query", ""),
                "refs": ",".join(args.get("refs") or []),
                "limit": args.get("limit", 8),
            }
            # httpx serialises None query values as an empty string. FastAPI then
            # correctly rejects that as an invalid datetime, so omit an absent
            # time cursor instead of sending ``as_of=`` on every normal search.
            if args.get("as_of"):
                params["as_of"] = args["as_of"]
            r = c.get("/v1/search", params=params)
        elif name == "memory_write":
            # `tier` is absent by design — the server assigns it from source_type.
            # An agent-authored memory is `inferred` and quarantined, which is the
            # entire point of ADR-0015 and cannot be delegated to the caller.
            r = c.post("/v1/memories", json={
                **scope, "type": args.get("type", "observation"),
                "title": args.get("title", ""), "content": args.get("content", ""),
                "source_type": "agent",
                "metadata": {"evidence": args.get("evidence") or [],
                             "op": args.get("op", "assert")},
            })
        elif name == "memory_explain":
            params = {**scope}
            if args.get("ref"):
                params["ref"] = args["ref"]
            if args.get("pack_id"):
                params["pack_id"] = args["pack_id"]
            r = c.get("/v1/explain", params=params)
        else:  # unreachable: names are validated before dispatch
            raise ValueError(name)

        r.raise_for_status()
        payload = r.json()

    # MCP tool results are content blocks. JSON goes in a text block as the
    # interoperable form; structuredContent carries the same payload for clients
    # that can use it, so neither kind of client is second-class.
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}],
        "structuredContent": payload,
        "isError": False,
    }


@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok",
        "protocol": PROTOCOL_VERSION,
        "api": API_URL,
        "auth": "oauth" if os.environ.get("MEMORY_OAUTH_ISSUER") else "none (dev binding)",
        "bound": bool(os.environ.get("MEMORY_OAUTH_ISSUER")
                      or (DEV_TENANT and DEV_PROJECT)),
    }
