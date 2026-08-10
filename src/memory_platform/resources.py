"""Read-only MCP resource materialization under an existing RLS scope.

The gateway resolves identity and scope, then delegates here through the API.
Keeping SQL out of the MCP process prevents resource reads from becoming a
second, less-audited copy of the authorization boundary.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, unquote, urlparse
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import conflicts


class InvalidResource(ValueError):
    """The caller supplied an unsupported or malformed memory URI."""


class ResourceNotFound(LookupError):
    """The URI is valid but does not resolve inside the current scope."""


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, default=str)


def _project(conn: Connection, tenant_id: UUID, project_id: UUID) -> dict:
    row = conn.execute(text(
        "SELECT id, slug, name FROM mem.projects "
        " WHERE id = :project AND tenant_id = :tenant"
    ), {"project": str(project_id), "tenant": str(tenant_id)}).mappings().one_or_none()
    if row is None:
        raise ResourceNotFound("project is not available in this scope")
    return dict(row)


def _uri_parts(uri: str) -> tuple[str, list[str]]:
    parsed = urlparse(uri)
    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidResource("resource URI has an invalid port") from exc
    if (parsed.scheme != "memory" or not parsed.netloc or parsed.params or
            parsed.query or parsed.fragment or parsed.username or parsed.password or
            port is not None):
        raise InvalidResource("resource URI must be a plain memory:// URI")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if any(not part or "/" in part for part in parts):
        raise InvalidResource("resource URI has an invalid path")
    return parsed.netloc, parts


def _resource(uri: str, mime_type: str, body: str) -> dict:
    return {"uri": uri, "mimeType": mime_type, "text": body}


def list_resources(conn: Connection, *, tenant_id: UUID, project_id: UUID) -> list[dict]:
    """Concrete, project-bound resources advertised by resources/list."""
    project = _project(conn, tenant_id, project_id)
    slug = quote(project["slug"], safe="")
    return [
        {
            "uri": f"memory://project/{slug}/profile",
            "name": "Project profile",
            "description": "Project identity, constraints, and stack from project.yaml.",
            "mimeType": "text/markdown",
        },
        {
            "uri": f"memory://project/{slug}/state",
            "name": "Project state",
            "description": "Current constraints, recent decisions, and unresolved-conflict count.",
            "mimeType": "text/markdown",
        },
        {
            "uri": f"memory://project/{slug}/timeline",
            "name": "Project timeline",
            "description": "Significant project events from the last 90 days.",
            "mimeType": "text/markdown",
        },
        {
            "uri": f"memory://project/{slug}/procedures",
            "name": "Procedure index",
            "description": "Procedure titles and memory references.",
            "mimeType": "text/markdown",
        },
        {
            "uri": f"memory://conflicts/{slug}",
            "name": "Unresolved conflicts",
            "description": "Contested points requiring human confirmation.",
            "mimeType": "application/json",
        },
    ]


def _profile(conn: Connection, project_id: UUID, uri: str) -> dict:
    row = conn.execute(text(
        "SELECT content FROM mem.memories "
        " WHERE project_id = :project AND source_uri = '.memory/project.yaml' "
        "   AND status = 'active' AND upper(valid_at) IS NULL "
        " ORDER BY recorded_at DESC LIMIT 1"
    ), {"project": str(project_id)}).mappings().one_or_none()
    if row is None:
        raise ResourceNotFound("project profile is not available")
    return _resource(uri, "text/markdown", row["content"])


def _state(conn: Connection, project: dict, uri: str) -> dict:
    rows = conn.execute(text(
        "SELECT id, title, digest, type::text AS type, tier::text AS tier, recorded_at "
        "  FROM mem.memories "
        " WHERE project_id = :project AND status = 'active' AND upper(valid_at) IS NULL "
        "   AND type::text = ANY(:types) "
        " ORDER BY recorded_at DESC, id DESC LIMIT 12"
    ), {"project": str(project["id"]),
         "types": ["constraint", "convention", "preference", "decision"]}).mappings().all()
    conflict_count = conn.execute(text(
        "SELECT count(*) FROM mem.conflicts "
        " WHERE project_id = :project AND resolution IS NULL"
    ), {"project": str(project["id"])}).scalar_one()

    lines = [f"# {project['name']} state", "", "## Current knowledge"]
    if rows:
        for row in rows:
            recorded = row["recorded_at"].date().isoformat()
            lines.append(
                f"- [{row['type']}] {row['title']} ({row['tier']}, {recorded}) "
                f"[memory://memory/{row['id']}]"
            )
    else:
        lines.append("- No active constraints or decisions are available.")
    lines.extend(["", "## Contested points", f"- {conflict_count} unresolved conflict(s)."])
    return _resource(uri, "text/markdown", "\n".join(lines) + "\n")


def _timeline(conn: Connection, project_id: UUID, uri: str) -> dict:
    rows = conn.execute(text(
        "SELECT id, title, digest, type::text AS type, tier::text AS tier, recorded_at "
        "  FROM mem.memories "
        " WHERE project_id = :project AND status = 'active' AND upper(valid_at) IS NULL "
        "   AND recorded_at >= now() - interval '90 days' "
        "   AND type::text = ANY(:types) "
        " ORDER BY recorded_at DESC, id DESC LIMIT 100"
    ), {"project": str(project_id),
         "types": ["decision", "episode", "failure", "success", "observation", "session_summary"]}).mappings().all()
    lines = ["# Significant events - last 90 days", ""]
    if not rows:
        lines.append("No significant events are available in this period.")
    for row in rows:
        recorded = row["recorded_at"].isoformat()
        lines.extend([
            f"## {recorded} - {row['type']}",
            f"[{row['title']}](memory://memory/{row['id']}) ({row['tier']})",
            row["digest"],
            "",
        ])
    return _resource(uri, "text/markdown", "\n".join(lines))


def _procedures(conn: Connection, project_id: UUID, uri: str) -> dict:
    rows = conn.execute(text(
        "SELECT id, title FROM mem.memories "
        " WHERE project_id = :project AND status = 'active' AND upper(valid_at) IS NULL "
        "   AND type = 'procedure' "
        " ORDER BY title, id"
    ), {"project": str(project_id)}).mappings().all()
    lines = ["# Procedure index", ""]
    if not rows:
        lines.append("No active procedures are available.")
    for row in rows:
        lines.append(f"- {row['title']} [memory://memory/{row['id']}]")
    return _resource(uri, "text/markdown", "\n".join(lines) + "\n")


def _memory(conn: Connection, uri: str, raw_id: str) -> dict:
    try:
        memory_id = str(UUID(raw_id))
    except ValueError as exc:
        raise InvalidResource("memory resource id must be a UUID") from exc
    row = conn.execute(text(
        "SELECT id, memory_key, title, content, digest, type::text AS type, "
        "       tier::text AS tier, status::text AS status, confidence, source_type, "
        "       source_uri, source_version, recorded_at, valid_at::text AS valid_at, "
        "       token_cost, content_hash, metadata "
        "  FROM mem.memories WHERE id = :id"
    ), {"id": memory_id}).mappings().one_or_none()
    if row is None:
        raise ResourceNotFound("memory is not available in this scope")
    memory = dict(row)
    memory["id"] = str(memory["id"])
    memory["recorded_at"] = memory["recorded_at"].isoformat()
    versions = conn.execute(text(
        "SELECT version, operation, changed_at FROM mem.memory_versions "
        " WHERE memory_id = :id ORDER BY version"
    ), {"id": memory_id}).mappings().all()
    supersessions = conn.execute(text(
        "SELECT old_id, new_id, reason, created_at FROM mem.memory_supersessions "
        " WHERE old_id = :id OR new_id = :id ORDER BY created_at"
    ), {"id": memory_id}).mappings().all()
    return _resource(uri, "application/json", _json({
        "memory": memory,
        "provenance": (
            f"{memory['source_type']}:{memory['source_uri']}@{memory['source_version']}"
            if memory.get("source_uri") else memory["source_type"]
        ),
        "versions": [
            {**dict(version), "changed_at": version["changed_at"].isoformat()}
            for version in versions
        ],
        "supersessions": [
            {**dict(edge), "old_id": str(edge["old_id"]), "new_id": str(edge["new_id"]),
             "created_at": edge["created_at"].isoformat()}
            for edge in supersessions
        ],
    }))


def _entity(conn: Connection, uri: str, raw_id: str) -> dict:
    try:
        entity_id = str(UUID(raw_id))
    except ValueError as exc:
        raise InvalidResource("entity resource id must be a UUID") from exc
    entity = conn.execute(text(
        "SELECT id, canonical_name, kind, attributes, tier::text AS tier, created_at "
        "  FROM mem.entities WHERE id = :id"
    ), {"id": entity_id}).mappings().one_or_none()
    if entity is None:
        raise ResourceNotFound("entity is not available in this scope")
    aliases = conn.execute(text(
        "SELECT alias FROM mem.entity_aliases WHERE entity_id = :id ORDER BY alias"
    ), {"id": entity_id}).scalars().all()
    relationships = conn.execute(text(
        "SELECT r.id, r.relation::text AS relation, r.tier::text AS tier, r.confidence, "
        "       r.evidence_memory_id, "
        "       CASE WHEN r.source_id = :id THEN 'outgoing' ELSE 'incoming' END AS direction, "
        "       CASE WHEN r.source_id = :id THEN target.id ELSE source.id END AS other_id, "
        "       CASE WHEN r.source_id = :id THEN target.canonical_name ELSE source.canonical_name END AS other "
        "  FROM mem.relationships r "
        "  JOIN mem.entities source ON source.id = r.source_id "
        "  JOIN mem.entities target ON target.id = r.target_id "
        " WHERE (r.source_id = :id OR r.target_id = :id) AND upper(r.valid_at) IS NULL "
        " ORDER BY r.created_at DESC, r.id"
    ), {"id": entity_id}).mappings().all()
    memories = conn.execute(text(
        "SELECT m.id, m.title, m.digest, m.type::text AS type, em.weight "
        "  FROM mem.entity_mentions em JOIN mem.memories m ON m.id = em.memory_id "
        " WHERE em.entity_id = :id AND m.status = 'active' AND upper(m.valid_at) IS NULL "
        " ORDER BY em.weight DESC, m.recorded_at DESC, m.id LIMIT 20"
    ), {"id": entity_id}).mappings().all()
    result = dict(entity)
    result["id"] = str(result["id"])
    result["created_at"] = result["created_at"].isoformat()
    return _resource(uri, "application/json", _json({
        "entity": result,
        "aliases": aliases,
        "relationships": [
            {**dict(item), "id": str(item["id"]), "other_id": str(item["other_id"]),
             "evidence_memory_id": str(item["evidence_memory_id"])
             if item["evidence_memory_id"] else None}
            for item in relationships
        ],
        "key_memories": [
            {**dict(item), "id": str(item["id"]),
             "uri": f"memory://memory/{item['id']}"}
            for item in memories
        ],
    }))


def _conflicts(conn: Connection, tenant_id: UUID, project_id: UUID, project: dict, uri: str) -> dict:
    return _resource(uri, "application/json", _json({
        "project": {"id": str(project["id"]), "slug": project["slug"], "name": project["name"]},
        "conflicts": conflicts.unresolved(
            conn, tenant_id=tenant_id, project_id=project_id, limit=50),
    }))


def read_resource(
    conn: Connection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    uri: str,
) -> dict:
    """Resolve one contract URI. Project segments must match the scoped project."""
    kind, parts = _uri_parts(uri)
    project = _project(conn, tenant_id, project_id)
    slug = project["slug"]
    if kind == "project" and len(parts) == 2 and parts[0] == slug:
        action = parts[1]
        if action == "profile":
            return _profile(conn, project_id, uri)
        if action == "state":
            return _state(conn, project, uri)
        if action == "timeline":
            return _timeline(conn, project_id, uri)
        if action == "procedures":
            return _procedures(conn, project_id, uri)
    if kind == "memory" and len(parts) == 1:
        return _memory(conn, uri, parts[0])
    if kind == "entity" and len(parts) == 1:
        return _entity(conn, uri, parts[0])
    if kind == "conflicts" and len(parts) == 1 and parts[0] == slug:
        return _conflicts(conn, tenant_id, project_id, project, uri)
    # Do not distinguish another scope's real slug from an unknown resource.
    raise ResourceNotFound("resource is not available in this scope")
