"""Graph endpoints — knowledge graph reads and edge review.

A SEPARATE ROUTER ON PURPOSE. Two agents working this repository kept colliding
in `api.py`, because every feature needs an endpoint and there was one file to
put it in. FastAPI already solves that: a router lives in its own module and
`api.py` gains one `include_router` line, added once and never touched again.
This module is Lane A's; api.py stays Lane B's.

Two jobs:

  * the neighbourhood read the console's graph screen needs, bounded to a small
    number of hops because the spec is explicit that the whole graph is never
    rendered — a full-graph view is a demo, a 2-hop neighbourhood is a tool;
  * accept / reject for proposed edges, which had no route at all: extraction
    had been writing proposals since the graph arm was built and nothing could
    act on them.
"""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from . import db, inbox

log = logging.getLogger("memory.routes.graph")

router = APIRouter(prefix="/v1/graph", tags=["graph"])

# Two hops covers "what does this touch, and what does that touch" and stops
# before the neighbourhood becomes the whole graph. Past three hops on a
# well-connected node you are returning everything, slowly.
MAX_HOPS = 3
MAX_NODES = 300


@router.get("/neighbourhood")
def neighbourhood(
    tenant_id: UUID,
    project_id: UUID,
    entity: str,
    principal_id: UUID | None = None,
    hops: int = 2,
    include_proposed: bool = False,
) -> dict:
    """Entities within `hops` of a named entity, with the edges between them.

    Accepted edges only by default. A proposed edge is a machine's guess, and
    rendering it beside a reviewed one without saying so would make the graph
    look more certain than it is — `include_proposed` returns them flagged.
    """
    hops = max(1, min(int(hops), MAX_HOPS))

    with db.scoped(tenant_id, principal_id or tenant_id, project_id) as conn:
        root = conn.execute(
            text("SELECT id, canonical_name, kind, tier::text AS tier "
                 "  FROM mem.entities "
                 " WHERE tenant_id = :t AND project_id = :p "
                 "   AND lower(canonical_name) = lower(:n) LIMIT 1"),
            {"t": str(tenant_id), "p": str(project_id), "n": entity},
        ).mappings().one_or_none()
        if root is None:
            raise HTTPException(404, f"no entity named {entity!r} in this scope")

        # Recursive walk in SQL rather than N round trips. `cycle` handling is
        # not optional: the graph is a graph, and A uses B uses A would recurse
        # until the statement timeout without it.
        rows = conn.execute(
            text("""
            WITH RECURSIVE walk(id, depth) AS (
                SELECT CAST(:root AS uuid), 0
              UNION
                SELECT CASE WHEN r.source_id = w.id THEN r.target_id
                            ELSE r.source_id END,
                       w.depth + 1
                  FROM walk w
                  JOIN mem.relationships r
                    ON (r.source_id = w.id OR r.target_id = w.id)
                 WHERE w.depth < :hops
            ) CYCLE id SET is_cycle USING path
            SELECT DISTINCT e.id, e.canonical_name, e.kind, e.tier::text AS tier,
                   MIN(w.depth) AS depth
              FROM walk w JOIN mem.entities e ON e.id = w.id
             GROUP BY e.id, e.canonical_name, e.kind, e.tier
             ORDER BY MIN(w.depth), e.canonical_name
             LIMIT :cap
            """),
            {"root": str(root["id"]), "hops": hops, "cap": MAX_NODES},
        ).mappings().all()

        ids = [str(r["id"]) for r in rows]
        edges = conn.execute(
            text("SELECT r.source_id, r.target_id, r.relation::text AS relation, "
                 "       r.confidence, r.tier::text AS tier, false AS proposed "
                 "  FROM mem.relationships r "
                 " WHERE r.tenant_id = :t "
                 "   AND r.source_id = ANY(CAST(:ids AS uuid[])) "
                 "   AND r.target_id = ANY(CAST(:ids AS uuid[]))"),
            {"t": str(tenant_id), "ids": "{" + ",".join(ids) + "}"},
        ).mappings().all() if ids else []

        proposed = []
        if include_proposed and ids:
            proposed = conn.execute(
                text("SELECT p.source_id, p.target_id, p.relation::text AS relation, "
                     "       p.confidence, p.tier::text AS tier, true AS proposed "
                     "  FROM mem.proposed_relationships p "
                     " WHERE p.tenant_id = :t AND p.decision IS NULL "
                     "   AND p.source_id = ANY(CAST(:ids AS uuid[])) "
                     "   AND p.target_id = ANY(CAST(:ids AS uuid[]))"),
                {"t": str(tenant_id), "ids": "{" + ",".join(ids) + "}"},
            ).mappings().all()

    return {
        "root": {"ref": str(root["id"]), "name": root["canonical_name"],
                 "kind": root["kind"], "tier": root["tier"]},
        "hops": hops,
        "nodes": [{"ref": str(r["id"]), "name": r["canonical_name"],
                   "kind": r["kind"], "tier": r["tier"], "depth": r["depth"]}
                  for r in rows],
        "edges": [{"source": str(e["source_id"]), "target": str(e["target_id"]),
                   "relation": e["relation"],
                   "confidence": round(float(e["confidence"]), 3),
                   "tier": e["tier"], "proposed": e["proposed"]}
                  for e in [*edges, *proposed]],
        # Surfaced so a truncated neighbourhood is visible rather than looking
        # like a sparsely connected entity.
        "truncated": len(rows) >= MAX_NODES,
    }


@router.get("/proposals")
def list_proposals(tenant_id: UUID, project_id: UUID,
                   principal_id: UUID | None = None, limit: int = 50) -> dict:
    """Edge proposals awaiting review — the same items the inbox surfaces."""
    with db.scoped(tenant_id, principal_id or tenant_id, project_id) as conn:
        items = inbox.list_items(conn, tenant_id=tenant_id,
                                 project_id=project_id, limit=limit)
    edges = [i for i in items["items"] if i["kind"] == "proposed_edge"]
    return {"count": len(edges), "proposals": edges}


class EdgeReview(BaseModel):
    tenant_id: UUID
    project_id: UUID
    principal_id: UUID
    ref: UUID
    action: str          # accept | reject
    reason: str = ""


@router.post("/proposals/review")
def review_proposal(req: EdgeReview) -> dict:
    """Accept or reject one proposed edge. Both outcomes are audited.

    Accepting writes a real relationship at `observed`; rejecting records the
    decision so the next extraction pass cannot put the same edge back in front
    of the same reviewer.
    """
    with db.scoped(req.tenant_id, req.principal_id, req.project_id) as conn:
        try:
            if req.action == "accept":
                return inbox.accept_edge(conn, tenant_id=req.tenant_id,
                                         proposal_id=req.ref,
                                         reviewer=req.principal_id)
            if req.action == "reject":
                return inbox.reject_edge(conn, tenant_id=req.tenant_id,
                                         proposal_id=req.ref,
                                         reviewer=req.principal_id,
                                         reason=req.reason)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
    raise HTTPException(422, "action must be accept or reject")
