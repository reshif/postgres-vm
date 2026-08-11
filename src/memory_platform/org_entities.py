"""Organisation-scoped entities — built, and off by default (ADR-0012).

    Build cross-project generalisation last, and ship it disabled by default.
    The mechanism is: generalise -> strip project-specific identifiers, URLs,
    hostnames and secrets -> propose -> human approve -> shared knowledge. Raw
    memories never cross project boundaries. Memories classified `restricted` are
    permanently excluded from generalisation, not merely excluded by default.

`mem.entities.project_id` was always nullable and the read policy always admitted
`project_id IS NULL`, so an organisation-scoped entity was already visible across
a tenant's projects. Nothing could create one. This module is the missing half,
and it is deliberately the *cautious* half:

WHAT CROSSES IS A NAME, NOT A MEMORY. Promotion shares the entity node — a
canonical name and kind, so two projects resolve "PgBouncer" to the same node.
Mentions, relationships and the memories behind them stay project-scoped. That
distinction is the reason this is safe enough to build at all: the graph gains a
shared vocabulary without any project's content becoming readable from another.

DISABLED BY DEFAULT MEANS THE PROPOSAL PATH IS CLOSED, NOT HIDDEN. With
`org_entities_enabled` false, `propose` refuses. It does not queue proposals that
would be silently promoted the day someone flips the flag.

THE SCREEN REJECTS, IT DOES NOT REDACT. A name containing a hostname or a path is
refused rather than cleaned up. Automatic redaction produces a plausible-looking
name that no longer means what the reviewer thinks it means, and the reviewer is
the control here — 05-BUILD-PLAN says the same about secrets: "hard reject, never
silent redaction".
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import secret_scan
from .config import settings

log = logging.getLogger("memory.org_entities")


class NotEnabled(RuntimeError):
    """Cross-project generalisation is off (ADR-0012's default)."""


class NotGeneralisable(ValueError):
    """The candidate carries project-specific detail and must not be shared."""


# Each pattern names something that identifies a PARTICULAR deployment rather
# than a concept. A shared vocabulary is the point; a shared hostname is a leak.
_PROJECT_SPECIFIC: list[tuple[str, re.Pattern[str]]] = [
    ("url", re.compile(r"\bhttps?://|\bwss?://|\bgit@", re.I)),
    ("hostname", re.compile(
        r"\b[\w-]+\.(com|net|org|io|dev|local|internal|cloud|ai|co)\b", re.I)),
    ("ip-address", re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")),
    ("filesystem-path", re.compile(r"(^|\s)(/[\w.-]+){2,}|[A-Za-z]:\\")),
    ("port", re.compile(r":\d{2,5}\b")),
    ("uuid", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                        r"[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)),
    ("commit-sha", re.compile(r"\b[0-9a-f]{7,40}\b", re.I)),
    ("env-var-value", re.compile(r"\b[A-Z][A-Z0-9_]{3,}=\S+")),
]

# Kinds that describe a deployment or an occurrence rather than a concept.
# An `incident` is by definition one project's event; a `technology` is not.
NON_GENERALISABLE_KINDS = {"incident", "environment", "person", "team"}


def screen(name: str, kind: str, attributes: dict[str, Any] | None = None
           ) -> list[str]:
    """Return the reasons this entity must not be shared. Empty means clean."""
    reasons: list[str] = []
    blob = f"{name}\n{json.dumps(attributes or {}, ensure_ascii=False)}"

    if kind in NON_GENERALISABLE_KINDS:
        reasons.append(f"kind `{kind}` describes one project's people, "
                       "environments or events, not shared knowledge")
    for label, pattern in _PROJECT_SPECIFIC:
        if pattern.search(blob):
            reasons.append(f"contains a project-specific {label}")
    # Secrets are a hard reject everywhere else on the write path; a name being
    # promoted to tenant-wide visibility is not the place to relax that.
    if secret_scan.scan(blob):
        reasons.append("matched the secret scanner")
    if len(name.strip()) < 2:
        reasons.append("name is too short to be a shared concept")
    return reasons


def _restricted_support(conn: Connection, entity_id: UUID, tenant_id: UUID) -> int:
    """Count supporting memories classified `restricted`.

    ADR-0012: restricted material is PERMANENTLY excluded from generalisation,
    not merely excluded by default. An entity is not its memories, but an entity
    that exists only because of restricted content should not become the shared
    vocabulary that leads another project to ask about it.

    Goes through mem.entity_restricted_support rather than counting directly. A
    plain count is subject to the sensitivity policy from 0023, which hides
    restricted rows from a session holding no grant for them — so the query
    returned 0 for exactly the entities it was supposed to catch, failing silent
    and permissive. The function is SECURITY DEFINER and returns a count and
    nothing else: enough to refuse the promotion, nothing about what is being
    protected.
    """
    return conn.execute(
        text("SELECT mem.entity_restricted_support(:e)"),
        {"e": str(entity_id)}).scalar_one()


def propose(conn: Connection, *, tenant_id: UUID, project_id: UUID,
            entity_id: UUID, proposed_name: str | None = None) -> dict[str, Any]:
    """Propose promoting one project entity to organisation scope."""
    if not settings().org_entities_enabled:
        raise NotEnabled(
            "cross-project generalisation is disabled (ADR-0012 ships it off). "
            "Set MEMORY_ORG_ENTITIES_ENABLED=true to open the proposal path.")

    row = conn.execute(
        text("SELECT id, kind, canonical_name, attributes, tier::text AS tier, "
             "       project_id "
             "  FROM mem.entities WHERE id = :e AND tenant_id = :t"),
        {"e": str(entity_id), "t": str(tenant_id)}).mappings().one_or_none()
    if row is None:
        raise LookupError(f"entity {entity_id} not found in this scope")
    if row["project_id"] is None:
        raise NotGeneralisable("entity is already organisation-scoped")

    name = (proposed_name or row["canonical_name"]).strip()
    reasons = screen(name, row["kind"], row["attributes"])
    restricted = _restricted_support(conn, entity_id, tenant_id)
    if restricted:
        reasons.append(
            f"supported by {restricted} memory(s) classified restricted, which "
            "ADR-0012 excludes from generalisation permanently")
    if reasons:
        raise NotGeneralisable("; ".join(reasons))

    proposal_id = conn.execute(
        text("INSERT INTO mem.proposed_org_entities "
             "  (tenant_id, project_id, entity_id, kind, canonical_name, "
             "   proposed_name, attributes, rationale) "
             "VALUES (:t, :p, :e, :k, :c, :n, CAST(:a AS jsonb), CAST(:r AS jsonb)) "
             "ON CONFLICT (tenant_id, entity_id) DO UPDATE "
             "  SET proposed_name = EXCLUDED.proposed_name, "
             "      rationale = EXCLUDED.rationale "
             "RETURNING id"),
        {"t": str(tenant_id), "p": str(project_id), "e": str(entity_id),
         "k": row["kind"], "c": row["canonical_name"], "n": name,
         "a": json.dumps(row["attributes"] or {}),
         "r": json.dumps({
             "screen": "passed",
             "checks": [label for label, _ in _PROJECT_SPECIFIC],
             "restricted_support": restricted,
             "shares": "entity node only; mentions, relationships and memories "
                       "remain project-scoped",
         })}).scalar_one()
    return {"proposal_id": str(proposal_id), "proposed_name": name,
            "kind": row["kind"]}


def review(conn: Connection, *, tenant_id: UUID, project_id: UUID,
           proposal_id: UUID, decision: str, principal_id: UUID | None = None,
           reason: str = "") -> dict[str, Any]:
    """Accept or reject a promotion. Accepting creates the org-scoped entity."""
    if decision not in ("accepted", "rejected"):
        raise ValueError("decision must be 'accepted' or 'rejected'")

    row = conn.execute(
        text("SELECT id, entity_id, kind, proposed_name, attributes, decision "
             "  FROM mem.proposed_org_entities "
             " WHERE id = :i AND tenant_id = :t AND project_id = :p"),
        {"i": str(proposal_id), "t": str(tenant_id),
         "p": str(project_id)}).mappings().one_or_none()
    if row is None:
        raise LookupError(f"proposal {proposal_id} not found in this scope")
    if row["decision"]:
        return {"proposal_id": str(proposal_id), "decision": row["decision"],
                "already_decided": True}

    org_entity_id = None
    if decision == "accepted":
        if not settings().org_entities_enabled:
            raise NotEnabled(
                "cross-project generalisation is disabled; refusing to promote")
        # Re-screened at the decision, not only at the proposal. A proposal can
        # sit in the queue while the entity is edited underneath it, and the
        # reviewer's approval refers to what they read.
        reasons = screen(row["proposed_name"], row["kind"], row["attributes"])
        if reasons:
            raise NotGeneralisable("; ".join(reasons))

        org_entity_id = conn.execute(
            text("INSERT INTO mem.entities "
                 "  (tenant_id, project_id, kind, canonical_name, tier, attributes) "
                 "VALUES (:t, NULL, :k, :n, 'observed', CAST(:a AS jsonb)) "
                 "ON CONFLICT (tenant_id, project_id, kind, canonical_name) "
                 "  DO UPDATE SET attributes = mem.entities.attributes "
                 "RETURNING id"),
            {"t": str(tenant_id), "k": row["kind"], "n": row["proposed_name"],
             "a": json.dumps({**(row["attributes"] or {}),
                              "shared": True, "origin": "promoted"})},
        ).scalar_one()

    conn.execute(
        text("UPDATE mem.proposed_org_entities "
             "   SET decision = :d, decided_at = now(), decided_by = :by, "
             "       reason = :reason "
             " WHERE id = :i"),
        {"d": decision, "by": str(principal_id) if principal_id else None,
         "reason": reason[:500], "i": str(proposal_id)})
    return {"proposal_id": str(proposal_id), "decision": decision,
            "org_entity_id": str(org_entity_id) if org_entity_id else None}


def pending(conn: Connection, *, tenant_id: UUID, project_id: UUID,
            limit: int = 50) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        text("SELECT id, entity_id, kind, canonical_name, proposed_name, "
             "       rationale, created_at "
             "  FROM mem.proposed_org_entities "
             " WHERE tenant_id = :t AND project_id = :p AND decision IS NULL "
             " ORDER BY created_at DESC LIMIT :limit"),
        {"t": str(tenant_id), "p": str(project_id), "limit": limit}).mappings().all()]
