"""Read-model queries for accepted GitHub-native evidence assertions.

The sidecar repository remains the authority. This module deliberately searches
the reviewed structured assertion fields only; it never turns an arbitrary Git
blob, webhook body, or assertion Markdown body into a retrievable document.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import memories

ASSERTION_REF_PREFIX = "assertion:"
_TERMS = re.compile(r"[A-Za-z0-9_./-]{3,}")


def is_github_native_project(conn: Connection, project_id: UUID) -> bool:
    provider = conn.execute(text(
        "SELECT source_provider FROM mem.projects WHERE id = :project"),
        {"project": str(project_id)}).scalar_one_or_none()
    return provider == "github"


def assertion_ref(assertion_id: UUID | str) -> str:
    return ASSERTION_REF_PREFIX + str(assertion_id)


def parse_assertion_ref(value: str) -> UUID | None:
    from uuid import UUID as ParseUUID

    if not value.startswith(ASSERTION_REF_PREFIX):
        return None
    try:
        return ParseUUID(value.removeprefix(ASSERTION_REF_PREFIX))
    except ValueError:
        return None


_BASE = """
SELECT a.id, a.assertion_key, a.subject, a.predicate, a.object_value,
       a.confidence, a.attributes, a.source_repository, a.source_path,
       a.source_revision, a.recorded_at, a.updated_at,
       coalesce(string_agg(DISTINCT support.source_repository || '@' ||
                           coalesce(support.source_revision, '') || ':' ||
                           coalesce(support.location, ''), E'\\n')
                FILTER (WHERE link.role = 'supports'), '') AS support_refs,
       {score} AS lexical_score
  FROM mem.evidence_assertions a
  LEFT JOIN mem.assertion_evidence link ON link.assertion_id = a.id
  LEFT JOIN mem.evidence_artifacts support ON support.id = link.artifact_id
 WHERE a.tenant_id = :tenant AND a.project_id = :project
   AND a.state = 'accepted' AND a.valid_at @> CAST(:as_of AS timestamptz)
   AND a.recorded_at <= CAST(:as_of AS timestamptz)
   AND {predicate}
 GROUP BY a.id
 ORDER BY lexical_score DESC, a.recorded_at DESC, a.id
 LIMIT :limit
"""


def _document() -> str:
    return "to_tsvector('english', concat_ws(' ', a.assertion_key, a.subject, a.predicate, a.object_value))"


def _row_to_candidate(row: dict[str, Any]) -> dict[str, Any]:
    attributes = dict(row.get("attributes") or {})
    subject = str(row["subject"])
    predicate = str(row["predicate"])
    object_value = str(row["object_value"])
    support_refs = [value for value in str(row.get("support_refs") or "").splitlines() if value]
    statement = f"{subject} {predicate} {object_value}"
    return {
        "id": row["id"],
        "ref": assertion_ref(row["id"]),
        "record_kind": "assertion",
        "assertion_key": row["assertion_key"],
        "title": f"{row['assertion_key']}: {statement}",
        "digest": f"Accepted GitHub evidence assertion: {statement}.",
        # This is structured, reviewed claim text. It intentionally excludes
        # the assertion Markdown body and every source blob body.
        "content": statement,
        "tier": "authoritative",
        "type": attributes.get("context_type", "entity_fact"),
        "status": "active",
        "token_cost": memories.count_tokens(statement),
        "importance_prior": float(row["confidence"]),
        "utility": 0.0,
        "retrieval_count": 0,
        "recorded_at": row["recorded_at"],
        "identifiers": " ".join((str(row["assertion_key"]), subject, predicate, object_value)),
        "source_uri": row["source_path"],
        "source_version": row["source_revision"],
        "source_repository": row["source_repository"],
        "support_refs": support_refs,
        "rrf_score": float(row.get("lexical_score") or 0.0),
        "r_vec": None,
        "r_lex": 1,
        "r_ident": None,
        "r_graph": None,
        "r_time": None,
        "dvec": None,
    }


def candidates(
    conn: Connection,
    query: str,
    *,
    tenant_id: UUID,
    project_id: UUID,
    limit: int,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return candidate assertions, strict lexical first then an OR fallback."""
    effective_as_of = as_of or datetime.now(timezone.utc)
    document = _document()
    strict = text(_BASE.format(
        score=f"ts_rank_cd({document}, websearch_to_tsquery('english', :query))",
        predicate=f"{document} @@ websearch_to_tsquery('english', :query)"))
    params = {"tenant": str(tenant_id), "project": str(project_id),
              "query": query, "as_of": effective_as_of, "limit": limit}
    rows = conn.execute(strict, params).mappings().all()
    if not rows:
        terms = [term for term in _TERMS.findall(query.lower()) if len(term) >= 3]
        if terms:
            tsquery = " | ".join(terms)
            relaxed = text(_BASE.format(
                score=f"ts_rank_cd({document}, to_tsquery('english', :fallback))",
                predicate=f"{document} @@ to_tsquery('english', :fallback)"))
            rows = conn.execute(relaxed, {**params, "fallback": tsquery}).mappings().all()
    return [_row_to_candidate(dict(row)) for row in rows]


def expand_refs(conn: Connection, refs: list[UUID]) -> list[dict[str, Any]]:
    """Expand assertion refs into claim and immutable provenance, never blob text."""
    if not refs:
        return []
    rows = conn.execute(text("""
        SELECT a.id, a.assertion_key, a.subject, a.predicate, a.object_value,
               a.confidence, a.attributes, a.state, a.source_repository,
               a.source_path, a.source_revision, a.recorded_at,
               coalesce(jsonb_agg(jsonb_build_object(
                 'role', link.role, 'repository', artifact.source_repository,
                 'revision', artifact.source_revision, 'path', artifact.location,
                 'content_sha256', artifact.content_sha256
               ) ORDER BY artifact.source_repository, artifact.location)
               FILTER (WHERE artifact.id IS NOT NULL), '[]'::jsonb) AS evidence
          FROM mem.evidence_assertions a
          LEFT JOIN mem.assertion_evidence link ON link.assertion_id = a.id
          LEFT JOIN mem.evidence_artifacts artifact ON artifact.id = link.artifact_id
         WHERE a.id = ANY(CAST(:ids AS uuid[])) AND a.state = 'accepted'
         GROUP BY a.id
         ORDER BY a.recorded_at DESC, a.id
    """), {"ids": "{" + ",".join(str(value) for value in refs) + "}"}).mappings().all()
    return [{
        "id": str(row["id"]), "ref": assertion_ref(row["id"]), "kind": "assertion",
        "assertion_key": row["assertion_key"], "subject": row["subject"],
        "predicate": row["predicate"], "object": row["object_value"],
        "type": dict(row["attributes"] or {}).get("context_type", "entity_fact"),
        "trust": "authoritative", "confidence": float(row["confidence"]),
        "state": row["state"], "source": {
            "repository": row["source_repository"], "revision": row["source_revision"],
            "path": row["source_path"],
        }, "evidence": list(row["evidence"] or []),
    } for row in rows]


def explain(conn: Connection, assertion_id: UUID) -> dict[str, Any] | None:
    rows = expand_refs(conn, [assertion_id])
    return rows[0] if rows else None
