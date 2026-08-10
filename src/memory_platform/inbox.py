"""Review inbox — the curation loop (Phase 5, ADR-0015).

Everything quarantined lands here: agent-written memories (`inferred`), anything
the injection heuristic flagged (`untrusted`), and unresolved conflicts. Nothing
leaves quarantine without a human decision.

ADR-0015 identifies the real risk as curation CAPACITY, not curation mechanism —
"the common failure is not bad code, it is that nobody triages the inbox in week
nine". Two consequences shape this module:

  * ORDERED BY WHAT IT COSTS TO IGNORE, not by arrival. An injection-flagged item
    is a security decision; an ordinary agent note is housekeeping. A strict FIFO
    queue buries the first behind fifty of the second.
  * AGE IS SURFACED, not hidden. `oldest_days` and a per-tenant backlog count are
    returned on every listing, because the failure mode is silent accumulation
    and the only defence is that the number is visible.

PROMOTION IS BOUNDED. A reviewer can raise an item to `observed` or `verified`.
They cannot mint `authoritative` — that tier means "reviewed in Plane A, in git,
with a diff" (ADR-0002), and a button that grants it would make the two-plane
model decorative. To make something authoritative you write the file.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

log = logging.getLogger("memory.inbox")

# What a reviewer may assign. `authoritative` is deliberately absent.
PROMOTABLE = ("observed", "verified")
REJECT_STATUS = "archived"

# Ordering weight: how expensive it is to leave this item unreviewed.
PRIORITY = {
    "injection": 0,       # a security decision
    "untrusted": 1,       # provenance we could not place
    "conflict": 2,        # two live claims disagree
    "inferred": 3,        # ordinary agent-written content
    # Below ordinary content on purpose. A wrong edge degrades retrieval
    # ranking; a wrong memory states something false to an agent. Both deserve
    # review, and when a reviewer has three minutes the memory goes first.
    "proposed_edge": 4,
}


def list_items(conn: Connection, *, tenant_id: UUID, project_id: UUID,
               limit: int = 50) -> dict[str, Any]:
    """Everything awaiting a human decision, most consequential first."""
    rows = conn.execute(
        text("SELECT id, title, digest, type::text AS type, tier::text AS tier, "
             "       status::text AS status, source_type, source_uri, "
             "       recorded_at, metadata, "
             "       EXTRACT(DAY FROM now() - recorded_at)::int AS age_days "
             "  FROM mem.memories "
             " WHERE tenant_id = :t AND project_id = :p "
             "   AND status = 'quarantined' AND upper(valid_at) IS NULL "
             " ORDER BY recorded_at "
             " LIMIT :k"),
        {"t": str(tenant_id), "p": str(project_id), "k": limit},
    ).mappings().all()

    items = []
    for r in rows:
        meta = r["metadata"] or {}
        kind = ("injection" if meta.get("injection")
                else "untrusted" if r["tier"] == "untrusted" else "inferred")
        items.append({
            "ref": str(r["id"]), "kind": kind, "title": r["title"],
            "digest": r["digest"], "type": r["type"], "tier": r["tier"],
            "source": r["source_type"], "source_uri": r["source_uri"],
            "age_days": r["age_days"],
            "why": meta.get("injection") or None,
            "recorded_at": r["recorded_at"].isoformat(),
        })

    conflicts = conn.execute(
        text("SELECT c.id, c.kind, a.title AS a_title, b.title AS b_title, "
             "       EXTRACT(DAY FROM now() - c.detected_at)::int AS age_days "
             "  FROM mem.conflicts c "
             "  JOIN mem.memories a ON a.id = c.memory_a "
             "  JOIN mem.memories b ON b.id = c.memory_b "
             " WHERE c.tenant_id = :t AND c.resolution IS NULL "
             " ORDER BY c.detected_at LIMIT :k"),
        {"t": str(tenant_id), "k": limit},
    ).mappings().all()
    for c in conflicts:
        items.append({
            "ref": str(c["id"]), "kind": "conflict", "title": c["a_title"],
            "digest": f"contested with: {c['b_title']}",
            "type": "conflict", "tier": None, "source": c["kind"],
            "source_uri": None, "age_days": c["age_days"], "why": [c["kind"]],
            "recorded_at": None,
        })

    # Inferred graph edges awaiting a human.
    #
    # These were stranded: entities.link_relations has been writing proposals
    # since the graph arm was built, and nothing ever read the table. 51 of them
    # had accumulated, invisible to the one screen whose job is to show a
    # reviewer what is waiting. The blueprint's "inferred edges land in the
    # inbox" was half-implemented — the producing half.
    edges = conn.execute(
        text("SELECT p.id, p.relation::text AS relation, p.confidence, "
             "       s.canonical_name AS source_name, s.kind AS source_kind, "
             "       t.canonical_name AS target_name, t.kind AS target_kind, "
             "       m.title AS evidence_title, p.evidence_memory_id, "
             "       EXTRACT(DAY FROM now() - p.proposed_at)::int AS age_days "
             "  FROM mem.proposed_relationships p "
             "  JOIN mem.entities s ON s.id = p.source_id "
             "  JOIN mem.entities t ON t.id = p.target_id "
             "  LEFT JOIN mem.memories m ON m.id = p.evidence_memory_id "
             " WHERE p.tenant_id = :t AND p.project_id = :p "
             "   AND p.decision IS NULL "
             " ORDER BY p.confidence DESC, p.proposed_at "
             " LIMIT :k"),
        {"t": str(tenant_id), "p": str(project_id), "k": limit},
    ).mappings().all()
    for e in edges:
        items.append({
            "ref": str(e["id"]), "kind": "proposed_edge",
            # Rendered as the claim itself. A reviewer deciding on an edge needs
            # to read the assertion, not look up two entity ids.
            "title": f"{e['source_name']} —{e['relation']}→ {e['target_name']}",
            "digest": (f"evidence: {e['evidence_title']}"
                       if e["evidence_title"] else "no evidence memory recorded"),
            "type": e["relation"], "tier": None,
            "source": f"{e['source_kind']} → {e['target_kind']}",
            "source_uri": None, "age_days": e["age_days"],
            "why": [f"confidence {e['confidence']:.2f}"],
            "recorded_at": None,
            "edge": {
                "source": e["source_name"], "target": e["target_name"],
                "relation": e["relation"], "confidence": round(e["confidence"], 3),
                "evidence_memory_id": (str(e["evidence_memory_id"])
                                       if e["evidence_memory_id"] else None),
            },
        })

    items.sort(key=lambda i: (PRIORITY.get(i["kind"], 9), -(i["age_days"] or 0)))

    total = conn.execute(
        text("SELECT count(*) FROM mem.memories WHERE tenant_id = :t "
             "  AND project_id = :p AND status = 'quarantined' "
             "  AND upper(valid_at) IS NULL"),
        {"t": str(tenant_id), "p": str(project_id)}).scalar_one()
    oldest = max((i["age_days"] or 0 for i in items), default=0)

    return {
        "count": len(items), "backlog": total, "oldest_days": oldest,
        # Surfaced, not buried: ADR-0015's failure mode is that nobody notices
        # the queue growing, so the queue reports on itself.
        "health": ("ok" if total < 25 and oldest < 14
                   else "backlog growing — triage capacity may be the constraint"),
        "items": items[:limit],
    }


def promote(conn: Connection, *, tenant_id: UUID, memory_id: UUID,
            to_tier: str, reviewer: UUID, note: str = "") -> dict[str, Any]:
    """Accept a quarantined memory at a reviewer-assignable tier."""
    if to_tier not in PROMOTABLE:
        raise ValueError(
            f"cannot promote to {to_tier!r}. A reviewer may assign "
            f"{' or '.join(PROMOTABLE)}; `authoritative` means reviewed in git "
            "(ADR-0002) and is earned by writing the file, not by clicking.")

    # `tier::text` on the right-hand side of SET is the OLD value, which is how
    # from_tier is captured without a second round trip. undo() needs it: a
    # restore that guesses the prior tier can silently raise trust.
    row = conn.execute(
        text("UPDATE mem.memories "
             "   SET tier = CAST(:tier AS mem.trust_tier), status = 'active', "
             "       confidence = :conf, "
             "       metadata = metadata || jsonb_build_object('review', "
             "         jsonb_build_object('action', 'promote', 'to', CAST(:tier AS text), "
             "                            'from_tier', tier::text, 'by', CAST(:by AS text), "
             "                            'note', CAST(:note AS text))) "
             " WHERE id = :i AND tenant_id = :t AND status = 'quarantined' "
             "RETURNING id, tier::text AS tier, status::text AS status"),
        {"i": str(memory_id), "t": str(tenant_id), "tier": to_tier,
         "conf": {"observed": 0.7, "verified": 0.85}[to_tier],
         "by": str(reviewer), "note": note[:500]},
    ).mappings().one_or_none()
    if row is None:
        raise LookupError("no quarantined memory with that id in this scope")

    _audit(conn, tenant_id, reviewer, "promote", memory_id,
           {"to_tier": to_tier, "note": note[:500]})
    log.info("promoted %s to %s by %s", memory_id, to_tier, reviewer)
    return dict(row) | {"id": str(row["id"])}


def reject(conn: Connection, *, tenant_id: UUID, memory_id: UUID,
           reviewer: UUID, reason: str = "") -> dict[str, Any]:
    """Reject a quarantined memory.

    Archived, never deleted, and its validity closed. The fact that something was
    proposed and refused is itself part of the record — and a deleted row cannot
    answer "did we already consider this?" the next time an agent proposes it.
    """
    row = conn.execute(
        text("UPDATE mem.memories "
             "   SET status = 'archived', superseded_at = now(), "
             "       valid_at = tstzrange(lower(valid_at), now(), '[)'), "
             "       metadata = metadata || jsonb_build_object('review', "
             "         jsonb_build_object('action', 'reject', 'from_tier', tier::text, "
             "                            'by', CAST(:by AS text), "
             "                            'reason', CAST(:reason AS text))) "
             " WHERE id = :i AND tenant_id = :t AND status = 'quarantined' "
             "RETURNING id, status::text AS status"),
        {"i": str(memory_id), "t": str(tenant_id),
         "by": str(reviewer), "reason": reason[:500]},
    ).mappings().one_or_none()
    if row is None:
        raise LookupError("no quarantined memory with that id in this scope")

    _audit(conn, tenant_id, reviewer, "reject", memory_id, {"reason": reason[:500]})
    return dict(row) | {"id": str(row["id"])}


def accept_edge(conn: Connection, *, tenant_id: UUID, proposal_id: UUID,
                reviewer: UUID) -> dict[str, Any]:
    """Promote a proposed graph edge into `mem.relationships`.

    The accepted edge lands at `observed`, never higher. §444 restricts real
    edges to tier >= 2, and a human confirming a machine's guess is exactly
    `observed` — it is not `authoritative`, because authoritative means written
    in git and reviewed as a diff (ADR-0002). An edge a reviewer can mint at the
    top tier from a button would make the same hole the memory path closes.
    """
    p = conn.execute(
        text("SELECT source_id, target_id, relation::text AS relation, "
             "       evidence_memory_id "
             "  FROM mem.proposed_relationships "
             " WHERE id = :i AND tenant_id = :t AND decision IS NULL"),
        {"i": str(proposal_id), "t": str(tenant_id)}).mappings().one_or_none()
    if p is None:
        raise LookupError("no pending edge proposal with that id in this scope")

    conn.execute(
        text("INSERT INTO mem.relationships "
             "  (tenant_id, project_id, source_id, target_id, relation, tier, "
             "   confidence, evidence_memory_id) "
             "SELECT :t, pr.project_id, pr.source_id, pr.target_id, pr.relation, "
             "       'observed', 0.7, pr.evidence_memory_id "
             "  FROM mem.proposed_relationships pr WHERE pr.id = :i "
             "ON CONFLICT DO NOTHING"),
        {"t": str(tenant_id), "i": str(proposal_id)})

    conn.execute(
        text("UPDATE mem.proposed_relationships "
             "   SET decision = 'accepted', reviewed_by = :by, reviewed_at = now() "
             " WHERE id = :i AND tenant_id = :t"),
        {"i": str(proposal_id), "t": str(tenant_id), "by": str(reviewer)})

    _audit(conn, tenant_id, reviewer, "accept_edge", proposal_id,
           {"relation": p["relation"], "source": str(p["source_id"]),
            "target": str(p["target_id"])})
    log.info("accepted edge %s (%s)", proposal_id, p["relation"])
    return {"id": str(proposal_id), "decision": "accepted",
            "relation": p["relation"]}


def reject_edge(conn: Connection, *, tenant_id: UUID, proposal_id: UUID,
                reviewer: UUID, reason: str = "") -> dict[str, Any]:
    """Reject a proposed edge. Recorded, never deleted.

    The record is what stops the next extraction pass re-proposing the same edge
    and the reviewer deciding it again — an inbox that re-asks answered questions
    is how curation capacity gets spent on nothing.
    """
    row = conn.execute(
        text("UPDATE mem.proposed_relationships "
             "   SET decision = 'rejected', reviewed_by = :by, "
             "       reviewed_at = now(), review_reason = :r "
             " WHERE id = :i AND tenant_id = :t AND decision IS NULL "
             "RETURNING id, relation::text AS relation"),
        {"i": str(proposal_id), "t": str(tenant_id), "by": str(reviewer),
         "r": reason[:500]}).mappings().one_or_none()
    if row is None:
        raise LookupError("no pending edge proposal with that id in this scope")

    _audit(conn, tenant_id, reviewer, "reject_edge", proposal_id,
           {"reason": reason[:500], "relation": row["relation"]})
    return {"id": str(row["id"]), "decision": "rejected",
            "relation": row["relation"]}


def unreview(conn: Connection, *, tenant_id: UUID, memory_id: UUID,
             reviewer: UUID) -> dict[str, Any]:
    """Undo a review decision — put the memory back in the queue.

    The console offers a 10-second undo, and the first implementation undid a
    promotion by REJECTING the memory. That is not an inverse: it archives
    something the reviewer merely mis-keyed, and it records a rejection reason
    that never happened. It also simply failed, because reject() requires
    `quarantined` and a promoted memory is `active`.

    The real inverse is to return the row to the queue at the tier it had before
    a human touched it. `metadata.review` carries what the decision was, so the
    restore does not have to guess; `inferred` is the fallback because that is
    where quarantined content starts.

    Reversible for BOTH promote and reject: a mis-keyed rejection is at least as
    likely as a mis-keyed acceptance, and the undo affordance promises the same
    thing in both cases.
    """
    prior = conn.execute(
        text("SELECT metadata, status::text AS status FROM mem.memories "
             " WHERE id = :i AND tenant_id = :t"),
        {"i": str(memory_id), "t": str(tenant_id)}).mappings().one_or_none()
    if prior is None:
        raise LookupError("no such memory in this scope")
    review = (prior["metadata"] or {}).get("review") or {}
    if not review:
        raise LookupError("that memory has no review decision to undo")

    row = conn.execute(
        text("UPDATE mem.memories "
             "   SET status = 'quarantined', "
             "       tier = CAST(:tier AS mem.trust_tier), "
             "       confidence = 0.4, superseded_at = NULL, "
             "       valid_at = tstzrange(lower(valid_at), NULL, '[)'), "
             "       metadata = metadata - 'review' "
             " WHERE id = :i AND tenant_id = :t "
             "RETURNING id, tier::text AS tier, status::text AS status"),
        {"i": str(memory_id), "t": str(tenant_id),
         "tier": review.get("from_tier") or "inferred"},
    ).mappings().one_or_none()
    if row is None:
        raise LookupError("no such memory in this scope")

    _audit(conn, tenant_id, reviewer, "undo", memory_id,
           {"undid": review.get("action"), "restored_tier": row["tier"]})
    log.info("undid %s on %s", review.get("action"), memory_id)
    return dict(row) | {"id": str(row["id"])}


def resolve_conflict(conn: Connection, *, tenant_id: UUID, conflict_id: UUID,
                     resolution: str, reviewer: UUID) -> dict[str, Any]:
    row = conn.execute(
        text("UPDATE mem.conflicts SET resolution = :r, resolved_by = :by, "
             "       resolved_at = now() "
             " WHERE id = :i AND tenant_id = :t AND resolution IS NULL "
             "RETURNING id"),
        {"i": str(conflict_id), "t": str(tenant_id), "r": resolution[:500],
         "by": str(reviewer)},
    ).mappings().one_or_none()
    if row is None:
        raise LookupError("no open conflict with that id in this scope")
    _audit(conn, tenant_id, reviewer, "resolve_conflict", conflict_id,
           {"resolution": resolution[:500]})
    return {"id": str(row["id"]), "resolved": True}


def _json(obj: dict) -> str:
    import json
    return json.dumps(obj)


def _audit(conn: Connection, tenant_id: UUID, principal: UUID, action: str,
           obj: UUID, detail: dict) -> None:
    """Every review decision is audited.

    Curation is the one place a human can raise the trust of machine-written
    content. An unaudited promotion is indistinguishable from a compromise.
    """
    project = conn.execute(text(
        "SELECT project_id FROM mem.memories WHERE id = :object "
        "UNION ALL "
        "SELECT project_id FROM mem.conflicts WHERE id = :object "
        "LIMIT 1"), {"object": str(obj)}).scalar_one_or_none()
    scope_context = {"tenant": str(tenant_id)}
    if project is not None:
        scope_context["project"] = str(project)
    conn.execute(
        text("INSERT INTO mem.audit_log "
             "  (tenant_id, principal_id, action, object_type, object_id, "
             "   scope_context, outcome, detail) "
             "VALUES (:t, :p, :a, 'memory', :o, CAST(:sc AS jsonb), 'allow', "
             "        CAST(:d AS jsonb))"),
        {"t": str(tenant_id), "p": str(principal), "a": f"review.{action}",
         "o": str(obj), "sc": _json(scope_context), "d": _json(detail)},
    )
