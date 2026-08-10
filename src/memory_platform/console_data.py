"""Read models for the Knowledge Console's explorer, timeline, and graph.

The console is intentionally a client of the same RLS-scoped API as MCP. These
queries stay here rather than in browser-specific handlers so the security and
bi-temporal rules have one implementation and can be exercised independently of
the UI.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection


MAX_EXPLORER_ROWS = 100
MAX_TIMELINE_ROWS = 1_000
MAX_GRAPH_NODES = 500
MAX_DASHBOARD_ROWS = 8


def _timestamps(row: dict[str, Any], *names: str) -> dict[str, Any]:
    """Return a JSON-safe mapping without turning database values into strings early."""
    out = dict(row)
    for name in names:
        value = out.get(name)
        if isinstance(value, datetime):
            out[name] = value.isoformat()
    return out


def _enum_filter(column: str, values: Iterable[str], key: str,
                 clauses: list[str], params: dict[str, Any]) -> None:
    selected = sorted({value.strip() for value in values if value.strip()})
    if selected:
        clauses.append(f"{column} = ANY(:{key})")
        params[key] = selected


def explorer(
    conn: Connection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    query: str = "",
    types: Iterable[str] = (),
    tiers: Iterable[str] = (),
    statuses: Iterable[str] = (),
    as_of: datetime | None = None,
    sort: str = "recorded_at",
    direction: str = "desc",
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """A bounded, sortable memory table with real bi-temporal filtering."""
    sort_columns = {
        "recorded_at": "m.recorded_at",
        "valid_from": "lower(m.valid_at)",
        "last_used": "m.last_accessed_at",
        "uses": "m.retrieval_count",
        "title": "m.title",
        "tokens": "m.token_cost",
        "type": "m.type::text",
    }
    if sort not in sort_columns:
        raise ValueError(f"unsupported explorer sort: {sort}")
    if direction.lower() not in {"asc", "desc"}:
        raise ValueError("explorer direction must be asc or desc")
    if offset < 0:
        raise ValueError("explorer offset must not be negative")
    if not 1 <= limit <= MAX_EXPLORER_ROWS:
        raise ValueError(f"explorer limit must be between 1 and {MAX_EXPLORER_ROWS}")

    clauses = ["m.tenant_id = :tenant", "m.project_id = :project"]
    params: dict[str, Any] = {
        "tenant": str(tenant_id), "project": str(project_id),
        "offset": offset, "limit": limit, "as_of": as_of,
    }
    if as_of is None:
        clauses.append("upper(m.valid_at) IS NULL")
    else:
        clauses.extend([
            "m.valid_at @> CAST(:as_of AS timestamptz)",
            "m.recorded_at <= CAST(:as_of AS timestamptz)",
        ])
        params["as_of"] = as_of
    if query.strip():
        clauses.append(
            "(m.content_tsv @@ websearch_to_tsquery('english', :query) "
            "OR m.title ILIKE :title_query)"
        )
        params["query"] = query.strip()
        params["title_query"] = f"%{query.strip()}%"
    _enum_filter("m.type::text", types, "types", clauses, params)
    _enum_filter("m.tier::text", tiers, "tiers", clauses, params)
    _enum_filter("m.status::text", statuses, "statuses", clauses, params)
    where = " AND ".join(clauses)
    order = sort_columns[sort]
    order_direction = direction.upper()

    total = conn.execute(text(
        f"SELECT count(*) FROM mem.memories m WHERE {where}"), params).scalar_one()
    rows = conn.execute(text(
        f"""
        SELECT m.id, m.title, m.digest, m.type::text AS type,
               m.tier::text AS tier, m.status::text AS status,
               m.scope_kind::text AS scope_kind, m.source_type, m.source_uri,
               m.source_version, m.recorded_at, lower(m.valid_at) AS valid_from,
               upper(m.valid_at) AS valid_until, m.last_accessed_at,
               m.retrieval_count, m.token_cost, m.pinned,
               CASE WHEN :as_of_present THEN m.valid_at @> CAST(:as_of AS timestamptz)
                    ELSE upper(m.valid_at) IS NULL END AS active_at_as_of
          FROM mem.memories m
         WHERE {where}
         ORDER BY {order} {order_direction} NULLS LAST, m.id {order_direction}
         OFFSET :offset LIMIT :limit
        """), {**params, "as_of_present": as_of is not None}).mappings().all()
    return {
        "total": int(total),
        "offset": offset,
        "limit": limit,
        "as_of": as_of.isoformat() if as_of else None,
        "items": [
            {**_timestamps(dict(row), "recorded_at", "valid_from", "valid_until", "last_accessed_at"),
             "id": str(row["id"])}
            for row in rows
        ],
    }


def dashboard(
    conn: Connection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    days: int = 30,
) -> dict[str, Any]:
    """Operational knowledge demand for one RLS-scoped project.

    ``retrieval_events`` is the source of truth here, rather than the denormalised
    ``memories.retrieval_count`` maintained by the scheduler.  That makes new
    demand visible immediately and lets the dashboard show questions with no
    returned evidence as a knowledge gap instead of silently losing them.
    """
    if not 7 <= days <= 90:
        raise ValueError("dashboard days must be between 7 and 90")

    params = {"tenant": str(tenant_id), "project": str(project_id), "days": days,
              "limit": MAX_DASHBOARD_ROWS}
    window = "created_at >= now() - make_interval(days => :days)"
    scope = "tenant_id = :tenant AND project_id = :project"
    answerability = "COALESCE(plan #>> '{answerability,status}', 'not_classified')"

    summary = conn.execute(text(
        f"""
        SELECT count(*)::int AS requests,
               count(DISTINCT NULLIF(btrim(query_text), ''))::int AS questions
          FROM mem.retrieval_events
         WHERE {scope} AND {window}
        """), params).mappings().one()

    outcomes = conn.execute(text(
        f"""
        SELECT {answerability} AS status, count(*)::int AS count
          FROM mem.retrieval_events
         WHERE {scope} AND {window}
         GROUP BY 1
         ORDER BY count DESC, status
        """), params).mappings().all()

    trend = conn.execute(text(
        """
        WITH calendar AS (
          SELECT generate_series(
                   date_trunc('day', now()) - make_interval(days => :days - 1),
                   date_trunc('day', now()), interval '1 day') AS day
        )
        SELECT calendar.day::date::text AS date,
               count(re.id)::int AS requests,
               count(DISTINCT NULLIF(btrim(re.query_text), ''))::int AS questions
          FROM calendar
          LEFT JOIN mem.retrieval_events re
            ON re.tenant_id = :tenant AND re.project_id = :project
           AND re.created_at >= calendar.day
           AND re.created_at < calendar.day + interval '1 day'
         GROUP BY calendar.day
         ORDER BY calendar.day
        """), params).mappings().all()

    questions = conn.execute(text(
        f"""
        SELECT query_text,
               count(*)::int AS requests,
               max(created_at) AS last_asked_at,
               (array_agg({answerability} ORDER BY created_at DESC))[1] AS answerability
          FROM mem.retrieval_events
         WHERE {scope} AND {window} AND NULLIF(btrim(query_text), '') IS NOT NULL
         GROUP BY query_text
         ORDER BY requests DESC, last_asked_at DESC, query_text
         LIMIT :limit
        """), params).mappings().all()

    knowledge = conn.execute(text(
        f"""
        WITH returned AS (
          SELECT memory_id, count(*)::int AS requests, max(re.created_at) AS last_used_at
            FROM mem.retrieval_events re
            CROSS JOIN LATERAL unnest(re.returned_ids) AS memory_id
           WHERE re.tenant_id = :tenant AND re.project_id = :project
             AND re.created_at >= now() - make_interval(days => :days)
           GROUP BY memory_id
        )
        SELECT m.id, m.title, m.type::text AS type, m.tier::text AS tier,
               returned.requests, returned.last_used_at
          FROM returned
          JOIN mem.memories m ON m.id = returned.memory_id
         WHERE m.tenant_id = :tenant AND m.project_id = :project
         ORDER BY returned.requests DESC, returned.last_used_at DESC, m.title, m.id
         LIMIT :limit
        """), params).mappings().all()

    return {
        "window_days": days,
        "summary": dict(summary),
        "outcomes": [dict(row) for row in outcomes],
        "trend": [dict(row) for row in trend],
        "top_questions": [
            {**_timestamps(dict(row), "last_asked_at")}
            for row in questions
        ],
        "top_knowledge": [
            {**_timestamps(dict(row), "last_used_at"), "id": str(row["id"])}
            for row in knowledge
        ],
    }


def timeline(
    conn: Connection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    as_of: datetime | None = None,
    limit: int = 250,
) -> dict[str, Any]:
    """Two temporal lanes: validity and when the platform learned the statement."""
    if not 1 <= limit <= MAX_TIMELINE_ROWS:
        raise ValueError(f"timeline limit must be between 1 and {MAX_TIMELINE_ROWS}")
    params: dict[str, Any] = {
        "tenant": str(tenant_id), "project": str(project_id), "limit": limit,
        "as_of": as_of,
    }
    learned_clause = "" if as_of is None else "AND m.recorded_at <= CAST(:as_of AS timestamptz)"
    rows = conn.execute(text(
        f"""
        SELECT m.id, m.title, m.digest, m.type::text AS type, m.tier::text AS tier,
               m.status::text AS status, m.source_uri, m.source_version,
               lower(m.valid_at) AS valid_from, upper(m.valid_at) AS valid_until,
               m.recorded_at,
               CASE WHEN CAST(:as_of AS timestamptz) IS NULL THEN upper(m.valid_at) IS NULL
                    ELSE m.valid_at @> CAST(:as_of AS timestamptz) END AS active_at_as_of
          FROM mem.memories m
         WHERE m.tenant_id = :tenant AND m.project_id = :project
           {learned_clause}
         ORDER BY lower(m.valid_at) DESC, m.recorded_at DESC, m.id DESC
         LIMIT :limit
        """), params).mappings().all()
    events = [
        {**_timestamps(dict(row), "valid_from", "valid_until", "recorded_at"),
         "id": str(row["id"])}
        for row in rows
    ]
    return {
        "as_of": as_of.isoformat() if as_of else None,
        "events": events,
        "valid_lane": sorted(events, key=lambda item: (item["valid_from"], item["id"])),
        "recorded_lane": sorted(events, key=lambda item: (item["recorded_at"], item["id"])),
    }


def graph(
    conn: Connection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    focus_id: UUID | None = None,
    query: str = "",
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Return a two-hop entity neighbourhood, never an unbounded full graph."""
    params: dict[str, Any] = {
        "tenant": str(tenant_id), "project": str(project_id), "as_of": as_of,
        "focus": str(focus_id) if focus_id else None,
    }
    suggestions = conn.execute(text(
        """
        SELECT e.id, e.canonical_name, e.kind, e.tier::text AS tier,
               count(DISTINCT em.memory_id)::int AS memory_count,
               (count(DISTINCT r.id) + count(DISTINCT p.id))::int AS relationship_count
          FROM mem.entities e
          LEFT JOIN mem.entity_mentions em ON em.entity_id = e.id
          LEFT JOIN mem.memories m ON m.id = em.memory_id
             AND (CAST(:as_of AS timestamptz) IS NULL OR (m.valid_at @> CAST(:as_of AS timestamptz)
                                  AND m.recorded_at <= CAST(:as_of AS timestamptz)))
          LEFT JOIN mem.relationships r
            ON (r.source_id = e.id OR r.target_id = e.id)
           AND r.tenant_id = :tenant AND r.project_id = :project
           AND (CAST(:as_of AS timestamptz) IS NULL
                OR r.valid_at @> CAST(:as_of AS timestamptz))
          LEFT JOIN mem.proposed_relationships p
            ON (p.source_id = e.id OR p.target_id = e.id)
           AND p.tenant_id = :tenant AND p.project_id = :project
           AND p.reviewed_by IS NULL
           AND (CAST(:as_of AS timestamptz) IS NULL
                OR p.valid_at @> CAST(:as_of AS timestamptz))
         WHERE e.tenant_id = :tenant AND e.project_id = :project
           AND (:query = '' OR e.canonical_name ILIKE :query_like)
         GROUP BY e.id
         ORDER BY relationship_count DESC, memory_count DESC, e.canonical_name, e.id
         LIMIT 30
        """), {**params, "query": query.strip(), "query_like": f"%{query.strip()}%"}).mappings().all()
    suggestion_data = [
        {**dict(row), "id": str(row["id"])}
        for row in suggestions
    ]
    if focus_id is None:
        return {"focus": None, "as_of": as_of.isoformat() if as_of else None,
                "suggestions": suggestion_data, "nodes": [], "edges": []}

    focus = conn.execute(text(
        """
        SELECT id, canonical_name, kind, tier::text AS tier
          FROM mem.entities
         WHERE id = :focus AND tenant_id = :tenant AND project_id = :project
        """), params).mappings().one_or_none()
    if focus is None:
        raise LookupError("entity is not available in this scope")

    edges = conn.execute(text(
        """
        WITH RECURSIVE neighbourhood(entity_id, depth) AS (
            SELECT CAST(:focus AS uuid), 0
            UNION
            SELECT CASE WHEN r.source_id = n.entity_id THEN r.target_id ELSE r.source_id END,
                   n.depth + 1
              FROM mem.relationships r
              JOIN neighbourhood n ON n.entity_id IN (r.source_id, r.target_id)
             WHERE n.depth < 2
               AND r.tenant_id = :tenant AND r.project_id = :project
               AND (CAST(:as_of AS timestamptz) IS NULL OR r.valid_at @> CAST(:as_of AS timestamptz))
        )
        SELECT DISTINCT r.id, r.source_id, r.target_id, r.relation::text AS relation,
               r.tier::text AS tier, r.confidence, r.evidence_memory_id,
               lower(r.valid_at) AS valid_from, upper(r.valid_at) AS valid_until,
               false AS proposed
          FROM mem.relationships r
         WHERE r.tenant_id = :tenant AND r.project_id = :project
           AND (r.source_id IN (SELECT entity_id FROM neighbourhood)
                OR r.target_id IN (SELECT entity_id FROM neighbourhood))
           AND (CAST(:as_of AS timestamptz) IS NULL OR r.valid_at @> CAST(:as_of AS timestamptz))
         ORDER BY r.confidence DESC, r.id
         LIMIT :limit
        """), {**params, "limit": MAX_GRAPH_NODES}).mappings().all()
    edge_data = [
        {**_timestamps(dict(row), "valid_from", "valid_until"),
         "id": str(row["id"]), "source_id": str(row["source_id"]),
         "target_id": str(row["target_id"]),
         "evidence_memory_id": str(row["evidence_memory_id"])
         if row["evidence_memory_id"] else None}
        for row in edges
    ]
    # Unreviewed relationships are deliberately a separate edge type. They are
    # useful hypotheses in the graph, but rendering them like observed facts is
    # the same category error as returning quarantined memory as guidance.
    proposed = conn.execute(text(
        """
        SELECT p.id, p.source_id, p.target_id, p.relation::text AS relation,
               p.tier::text AS tier, p.confidence, p.evidence_memory_id,
               lower(p.valid_at) AS valid_from, upper(p.valid_at) AS valid_until,
               true AS proposed
          FROM mem.proposed_relationships p
         WHERE p.tenant_id = :tenant AND p.project_id = :project
           AND p.reviewed_by IS NULL
           AND (p.source_id = CAST(:focus AS uuid) OR p.target_id = CAST(:focus AS uuid))
           AND (CAST(:as_of AS timestamptz) IS NULL
                OR p.valid_at @> CAST(:as_of AS timestamptz))
         ORDER BY p.confidence DESC, p.id
         LIMIT :limit
        """), {**params, "limit": MAX_GRAPH_NODES}).mappings().all()
    edge_data.extend(
        {**_timestamps(dict(row), "valid_from", "valid_until"),
         "id": str(row["id"]), "source_id": str(row["source_id"]),
         "target_id": str(row["target_id"]),
         "evidence_memory_id": str(row["evidence_memory_id"])
         if row["evidence_memory_id"] else None}
        for row in proposed
    )
    node_ids = {str(focus["id"])}
    for edge in edge_data:
        node_ids.add(edge["source_id"])
        node_ids.add(edge["target_id"])
    nodes = conn.execute(text(
        """
        SELECT e.id, e.canonical_name, e.kind, e.tier::text AS tier,
               count(em.memory_id)::int AS memory_count
          FROM mem.entities e
          LEFT JOIN mem.entity_mentions em ON em.entity_id = e.id
         WHERE e.id = ANY(CAST(:ids AS uuid[]))
         GROUP BY e.id
         ORDER BY e.canonical_name, e.id
        """), {"ids": "{" + ",".join(sorted(node_ids)) + "}"}).mappings().all()
    return {
        "focus": {**dict(focus), "id": str(focus["id"])},
        "as_of": as_of.isoformat() if as_of else None,
        "suggestions": suggestion_data,
        "nodes": [{**dict(row), "id": str(row["id"])} for row in nodes],
        "edges": edge_data,
    }
