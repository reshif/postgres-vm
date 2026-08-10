"""Conflict detection — filling the pack's `contested` section.

00-MASTER-BLUEPRINT.md §440: "Unresolved conflicts are surfaced *in the context
pack itself* as `⚠ contested`, with both sides and dates. An agent told 'we
contest this — check with a human' behaves far better than one confidently given
the loser."

The pack has had a `contested` section since it was written and nothing ever
filled it, so every pack silently claimed there were no disagreements.

DETERMINISTIC, AND HONEST ABOUT WHAT THAT CAN DO. ADR-0015 forbids LLM
extraction until a curator exists, and "do these two paragraphs contradict each
other" is genuinely a language problem. So this detects CANDIDATES using signals
that are cheap and checkable, and every one lands in mem.conflicts for a human:

  1. DANGLING SUPERSESSION — a document declares `supersedes: ADR-0003` in its
     frontmatter and ADR-0003 is still active. That is not a guess; it is a
     stated contradiction that ingestion failed to act on.
  2. NEGATION PAIR — two active decisions share an entity, and one asserts what
     the other negates ("use Redis" / "no Redis", "we will" / "we will not").
  3. NEAR-MISS SIMILARITY — two decisions share an entity and their digests sit
     in the band between "related" and "duplicate" (0.80–0.94 cosine). Same
     subject, different text. Weakest signal, lowest confidence, and it is why
     these are candidates rather than conclusions.

WHY A CANDIDATE IS STILL WORTH SURFACING. A false positive costs an agent one
line of "these two may disagree, check with a human". A false negative hands it
the losing side of a settled argument with full confidence. The asymmetry is the
whole reason this runs at all.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

log = logging.getLogger("memory.conflicts")

SIM_LOWER = 0.80    # below this they are simply different topics
SIM_UPPER = 0.94    # at/above this MMR already treats them as duplicates
CONFLICTING_TYPES = ("decision", "constraint", "convention", "procedure")

# Assertion / negation pairs. Kept explicit rather than clever: a stemmer that
# decides "unsupported" negates "supported" produces conflicts nobody can explain.
NEGATIONS: list[tuple[re.Pattern[str], re.Pattern[str]]] = [
    (re.compile(r"\bwe (will|should|do) use\b", re.I),
     re.compile(r"\bwe (will not|won't|should not|shouldn't|do not|don't) use\b", re.I)),
    (re.compile(r"\badopt(ed|ing)?\b", re.I), re.compile(r"\breject(ed|ing)?\b", re.I)),
    (re.compile(r"\benable[sd]?\b", re.I), re.compile(r"\bdisable[sd]?\b", re.I)),
    (re.compile(r"\brequired?\b", re.I), re.compile(r"\boptional\b", re.I)),
    (re.compile(r"\bforbidden\b", re.I), re.compile(r"\ballowed\b", re.I)),
]
# NOT included: always/never and keep/remove.
#
# They are too generic to be evidence even with the proximity rule. Two deploy
# procedures that both mention Docker — one saying "always run migrations", the
# other "never skip review" — matched, and a conflict a reviewer must read and
# dismiss costs exactly what a real one costs. ADR-0015 makes reviewer attention
# the scarce resource, so the bar for spending it is a phrase that is ABOUT a
# decision on the subject, not a modal adverb sitting near it.


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return num / (na * nb) if na and nb else 0.0


def _parse_vec(s: str | None) -> list[float]:
    if not s:
        return []
    try:
        return [float(x) for x in s.strip("[]").split(",") if x]
    except ValueError:
        return []


NEAR_CHARS = 140


def negation_pair(a: str, b: str, subjects: list[str] | None = None) -> str | None:
    """True only when the two texts disagree ABOUT THE SAME SUBJECT.

    The first version asked whether text A contained "always" anywhere and text B
    contained "never" anywhere. Applied to real documents that is a
    false-positive machine: every runbook says "always" somewhere and every ADR
    says "never" somewhere, so it paired a conventions file with a deploy
    procedure and two unrelated ADRs. The review inbox surfaced them
    immediately — which is what an inbox is for, and also why an unprecise
    detector is worse than none: it spends the reviewer's attention, and
    attention is the scarce resource ADR-0015 is about.

    So a hit now requires the asserting phrase and the negating phrase to each
    sit within NEAR_CHARS of a mention of the SAME entity. "We will use Redis"
    against "We will not use Redis" still matches; "always run migrations" in one
    file against "never skip review" in another does not.
    """
    if not subjects:
        return None
    la, lb = a.lower(), b.lower()

    def near(text_: str, pat, subject: str) -> bool:
        subj = subject.lower()
        for m in pat.finditer(text_):
            lo, hi = max(0, m.start() - NEAR_CHARS), m.end() + NEAR_CHARS
            if subj in text_[lo:hi]:
                return True
        return False

    for subject in subjects:
        if subject.lower() not in la or subject.lower() not in lb:
            continue
        for pos, neg in NEGATIONS:
            if ((near(la, pos, subject) and near(lb, neg, subject))
                    or (near(la, neg, subject) and near(lb, pos, subject))):
                return f"{subject}: {pos.pattern[:18]} vs {neg.pattern[:18]}"
    return None


CANDIDATES = text("""
SELECT m.id, m.title, m.digest, m.content, m.type::text AS type,
       m.tier::text AS tier, m.recorded_at, m.metadata,
       e.digest_embedding::text AS dvec,
       (SELECT array_agg(em.entity_id) FROM mem.entity_mentions em
         WHERE em.memory_id = m.id) AS entities
  FROM mem.memories m
  LEFT JOIN mem.memory_embeddings e ON e.memory_id = m.id
 WHERE m.tenant_id = :t AND m.project_id = :p
   AND m.status = 'active' AND upper(m.valid_at) IS NULL
   AND m.type::text = ANY(:types)
""")


def detect(
    conn: Connection, *, tenant_id: UUID, project_id: UUID, limit_pairs: int = 4000,
) -> dict[str, Any]:
    """Find conflict candidates and record them. Idempotent."""
    rows = [dict(r) for r in conn.execute(
        CANDIDATES, {"t": str(tenant_id), "p": str(project_id),
                     "types": list(CONFLICTING_TYPES)}).mappings().all()]
    by_id = {str(r["id"]): r for r in rows}

    # 1. Dangling supersessions: a stated contradiction, not an inferred one.
    found: list[tuple[str, str, str, float]] = []
    key_by_ident: dict[str, str] = {}
    for r in rows:
        ident = (r["metadata"] or {}).get("id")
        if ident:
            key_by_ident[str(ident).lower()] = str(r["id"])
    for r in rows:
        sup = (r["metadata"] or {}).get("supersedes")
        for target in ([sup] if isinstance(sup, str) else (sup or [])):
            tid = key_by_ident.get(str(target).lower())
            if tid and tid != str(r["id"]):
                found.append((str(r["id"]), tid, "dangling-supersession", 0.95))

    # 2/3. Entity-sharing pairs. Restricted to pairs that share an entity so this
    # is O(pairs that plausibly discuss the same thing) rather than O(n^2) over
    # the whole corpus.
    vectors = {str(r["id"]): _parse_vec(r.get("dvec")) for r in rows}
    ents = {str(r["id"]): set(r["entities"] or []) for r in rows}
    ids = list(by_id)
    pairs = 0
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if pairs >= limit_pairs:
                break
            if not (ents[a] & ents[b]):
                continue
            pairs += 1
            ta = f"{by_id[a]['title']} {by_id[a]['content']}"
            tb = f"{by_id[b]['title']} {by_id[b]['content']}"

            # Shared entity names are the candidate subjects for disagreement.
            shared = conn.execute(
                text("SELECT e.canonical_name FROM mem.entities e "
                     " WHERE e.id = ANY(:ids)"),
                {"ids": list(ents[a] & ents[b])}).scalars().all()
            label = negation_pair(ta, tb, list(shared))
            if label:
                found.append((a, b, f"negation:{label}", 0.7))
                continue

            sim = _cosine(vectors.get(a, []), vectors.get(b, []))
            if SIM_LOWER <= sim < SIM_UPPER:
                found.append((a, b, "near-duplicate-divergence", round(sim, 3)))

    recorded = 0
    for a, b, kind, confidence in found:
        # Order the pair so (a,b) and (b,a) are the same row.
        lo, hi = sorted((a, b))
        res = conn.execute(
            text("INSERT INTO mem.conflicts "
                 "  (tenant_id, project_id, memory_a, memory_b, kind, detected_by) "
                 "SELECT :t, :p, :a, :b, :k, 'deterministic' "
                 " WHERE NOT EXISTS (SELECT 1 FROM mem.conflicts c "
                 "   WHERE c.memory_a = :a AND c.memory_b = :b AND c.kind = :k) "
                 "RETURNING id"),
            {"t": str(tenant_id), "p": str(project_id), "a": lo, "b": hi, "k": kind},
        ).scalar_one_or_none()
        if res:
            recorded += 1
            log.info("conflict candidate (%s, %.2f): %s <-> %s", kind, confidence,
                     by_id[lo]["title"][:40], by_id[hi]["title"][:40])

    return {"examined": len(rows), "pairs": pairs,
            "candidates": len(found), "recorded": recorded}


def unresolved(
    conn: Connection, *, tenant_id: UUID, project_id: UUID, limit: int = 10,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    """Open conflicts, rendered for the pack's `contested` section.

    Both sides and both dates, per §440 — an agent shown only one side of a
    contested point is in a worse position than one shown neither.
    """
    rows = conn.execute(
        text("SELECT c.id, c.kind, c.detected_at, "
             "       a.id AS a_id, a.title AS a_title, a.digest AS a_digest, "
             "       a.tier::text AS a_tier, a.recorded_at AS a_at, "
             "       b.id AS b_id, b.title AS b_title, b.digest AS b_digest, "
             "       b.tier::text AS b_tier, b.recorded_at AS b_at "
             "  FROM mem.conflicts c "
             "  JOIN mem.memories a ON a.id = c.memory_a "
             "  JOIN mem.memories b ON b.id = c.memory_b "
             " WHERE c.tenant_id = :t AND c.project_id = :p "
              "   AND (CAST(:as_of AS timestamptz) IS NOT NULL AND c.detected_at <= CAST(:as_of AS timestamptz) "
             "        AND (c.resolved_at IS NULL OR c.resolved_at > CAST(:as_of AS timestamptz)) "
             "        AND a.valid_at @> CAST(:as_of AS timestamptz) "
             "        AND b.valid_at @> CAST(:as_of AS timestamptz) "
             "        AND a.recorded_at <= CAST(:as_of AS timestamptz) "
             "        AND b.recorded_at <= CAST(:as_of AS timestamptz) "
             "      OR :as_of IS NULL AND c.resolution IS NULL "
             "        AND a.status = 'active' AND b.status = 'active') "
             " ORDER BY c.detected_at DESC LIMIT :k"),
        {"t": str(tenant_id), "p": str(project_id), "k": limit, "as_of": as_of},
    ).mappings().all()

    out = []
    for r in rows:
        out.append({
            "conflict_id": str(r["id"]),
            "kind": r["kind"],
            "sides": [
                {"ref": str(r["a_id"]), "title": r["a_title"], "digest": r["a_digest"],
                 "trust": r["a_tier"], "recorded_at": r["a_at"].isoformat()},
                {"ref": str(r["b_id"]), "title": r["b_title"], "digest": r["b_digest"],
                 "trust": r["b_tier"], "recorded_at": r["b_at"].isoformat()},
            ],
            "note": ("Unresolved — confirm with a human before relying on either "
                     "side."),
        })
    return out
