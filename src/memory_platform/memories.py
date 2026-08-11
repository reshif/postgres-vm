"""Memory write path — provenance, trust tier, hashing, token cost.

This is the engine behind `memory.write`; the MCP tool in Phase 4 is a thin
adapter over it. Everything here runs inside db.scoped(), so RLS decides what is
writable and this module never has to re-implement scope checks.

THE TRUST TIER IS ASSIGNED HERE, FROM THE SOURCE, AND NEVER TAKEN FROM THE
CALLER. 02-MCP-CONTRACT.md states it as a property of the tool, but a contract
enforced only at the edge is not enforced: the tier is what retrieval filters on
(defaults to >= observed) and what the ranking function weights, so a caller able
to set it could promote its own text to authoritative. The mapping follows the
lattice in 00-MASTER-BLUEPRINT.md §383.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import embeddings
from .config import settings

log = logging.getLogger("memory.memories")

MAX_CONTENT = 8000   # mem.memories_content_check
MAX_DIGEST = 400     # mem.memories_digest_check

# Retrieval always has a nearest neighbour. That is an ordering fact, not
# evidence that the project contains an answer. These stop words leave the
# domain-bearing terms used by the explicit no-evidence decision below.
_EVIDENCE_TOKEN = re.compile(r"[A-Za-z0-9_./-]+")
_EVIDENCE_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "at", "be", "by", "can", "do", "does",
    "did", "for", "from", "how", "i", "in", "is", "it", "of", "on",
    "or", "should", "the", "to", "us", "was", "we", "what", "when",
    "where", "which", "who", "why", "with", "you", "your",
    # Question scaffolding is not evidence. Keeping it made a longer natural
    # query require three or four matches even when it named two precise
    # project terms such as Postgres and memory.
    "actually", "another", "before", "being", "ever", "get", "give", "has", "have",
    "many", "me", "most", "one", "rather", "see", "some", "than", "this",
    "three", "use", "went", "would",
})

# Human-reviewed vocabulary bridges used in this project. These are deliberately
# small semantic families, not an embedding score masquerading as proof: each
# member is an established name for the same concrete project concept. A match
# is still subject to the normal per-clause threshold and is recorded in the
# pack's evidence explanation.
_EVIDENCE_TERM_FAMILIES = (
    frozenset({"broker", "message", "queue", "job"}),
    frozenset({"cache", "datastore", "database", "redis", "store"}),
    frozenset({"provider", "vendor"}),
    frozenset({"rely", "uses"}),
    frozenset({"importance", "matter", "priority", "utility"}),
    frozenset({"belief", "believe", "history", "temporal"}),
    frozenset({"generalisation", "generalization", "share"}),
    frozenset({"eval", "evaluation"}),
    frozenset({"second", "separate"}),
    frozenset({"debug", "troubleshoot"}),
)
_TERM_STEMS = {
    "believed": "believe",
    "blocked": "block",
    "caching": "cache",
    "computed": "compute",
    "dots": "dot",
    "embedder": "embed",
    "embedding": "embed",
    "exited": "exit",
    "killed": "kill",
    "learned": "learn",
    "ranking": "rank",
    "ranked": "rank",
    "ranker": "rank",
    "relies": "rely",
    "sharing": "share",
    "sigkilled": "kill",
    "stripped": "strip",
    "updated": "update",
}
_GENERIC_LONG_TERMS = frozenset({
    "archive", "content", "decision", "document", "information", "policy",
    "procedure", "project", "required", "retention", "service", "source",
    "storage", "system", "version",
})
_GENERIC_DIRECT_TERMS = _GENERIC_LONG_TERMS | frozenset({"define", "tell", "used", "working"})
_RELATIONSHIP_QUERY = re.compile(
    r"\b(depends?|dependenc(?:y|ies)|impact|affects?|related|relationship|"
    r"what\s+breaks|uses?|used\s+by)\b",
    re.IGNORECASE,
)
_COMPOUND_QUESTION = re.compile(
    r"\s+\band\s+(?=(?:how|why|what|when|where|which|who|can|does|do|is|are)\b)",
    re.IGNORECASE,
)

# Source of the claim -> trust tier. See 00-MASTER-BLUEPRINT.md:383-386.
#   authoritative  human-authored or human-approved, in Plane A (git)
#   verified       system-verified outcome: CI passed, deploy succeeded
#   observed       deterministic capture of what happened
#   inferred       LLM extraction from trusted-source text -> QUARANTINE
#   untrusted      anything whose provenance we cannot place
TIER_BY_SOURCE: dict[str, str] = {
    "git": "authoritative",      # committed .memory/ content, reviewed in a PR
    "human": "authoritative",    # explicit human authorship through the console
    "ci": "verified",
    "deploy": "verified",
    "test": "verified",
    "commit": "observed",
    "tool": "observed",
    "capture": "observed",
    "agent": "inferred",
    "extraction": "inferred",
    # A consolidation summary is mechanically derived from memories that were
    # already at `observed` or better (consolidation only ever reads active rows,
    # and quarantined material is not active). It is deliberately pinned to
    # `observed` rather than inheriting the best member's tier: the summary is
    # extractive and reproducible, but it is still an aggregate nobody reviewed,
    # so it must not be able to launder a group of verified memories into a
    # verified claim of its own. It must also not land in quarantine — compaction
    # archives the originals, so a quarantined summary would remove the whole
    # group from retrieval and replace it with nothing.
    "consolidation": "observed",
}

# Tier 1 and below never reach an agent as normal knowledge. ADR-0015 caps
# extraction at a reviewable tier precisely so a poisoned source cannot become
# standing instruction; quarantining here is what makes that cap real.
QUARANTINE_TIERS = {"inferred", "untrusted"}

CONFIDENCE_BY_TIER = {
    "authoritative": 0.95,
    "verified": 0.85,
    "observed": 0.7,
    "inferred": 0.4,
    "untrusted": 0.2,
}

# Symbols, paths, error codes — the trigram arm searches this column, so it wants
# the tokens a developer would actually paste: dotted.paths, snake_case,
# CamelCase, file/paths.py, ERR_CODES, and bare 4xx/5xx-looking numbers.
_IDENT = re.compile(
    r"""(?x)
    \b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b   # dotted.path
  | \b[a-z0-9_]+\.[a-z]{1,5}\b                                 # file.ext
  | \b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b                      # CamelCase
  | \b[A-Z][A-Z0-9_]{2,}\b                                     # ERR_CONSTANT
  | \b[a-z]+(?:_[a-z0-9]+)+\b                                  # snake_case
    """
)


def content_hash(content: str) -> str:
    """Idempotency key. Whitespace-normalised so a reformatted file that says the
    same thing does not create a second memory (Phase 1: "re-running ingestion
    creates no duplicates")."""
    return hashlib.sha256(" ".join(content.split()).encode("utf-8")).hexdigest()


def count_tokens(txt: str) -> int:
    """Approximate token cost.

    Deliberately a heuristic, not a tokenizer. The real budget maths lands in
    Phase 3 with the context engine, and the right tokenizer then is whichever
    model consumes the pack — pinning one now would encode the wrong model's
    vocabulary into stored rows. ~4 chars/token is the standard approximation and
    is honest to within ~10-15% for English prose and code.
    """
    return max(1, (len(txt) + 3) // 4)


def _evidence_terms(text: str) -> set[str]:
    """Return the query-bearing terms used to justify a returned memory."""
    terms: set[str] = set()
    for raw in _EVIDENCE_TOKEN.findall(text or ""):
        # A hyphenated value can be a generated marker or error identifier, not
        # merely three ordinary words. Keep its whole form in addition to the
        # components so `mcp-foreign-abc123` cannot be justified by unrelated
        # local documents that happen to mention MCP, foreign, and abc123.
        if "-" in raw:
            whole = raw.strip("-./").lower()
            if len(whole) > 1 and whole not in _EVIDENCE_STOP_WORDS:
                terms.add(whole)
        # Symbols and file-like identifiers are composed from their parts for
        # evidence purposes. `memory_timeline`, `memory.explain` and
        # `max_input_length` must match source text that may use spaces, dots,
        # or underscores without making a single opaque token the whole query.
        parts = re.split(r"[_.\-/]+", raw)
        for part in parts:
            token = part.lower()
            if (len(token) <= 1 and not token.isdigit()) or token in _EVIDENCE_STOP_WORDS:
                continue
            token = _TERM_STEMS.get(token, token)
            if token.endswith("ies") and len(token) > 4:
                token = token[:-3] + "y"
            elif (token.endswith("s") and len(token) > 4 and not raw.isupper()
                  and token not in {"postgres", "redis", "status"}
                  and not token.endswith("ss")):
                token = token[:-1]
            terms.add(token)
    return terms


def _candidate_evidence_terms(text: str) -> set[str]:
    """Expand a few unambiguous storage-name forms present in project memory."""
    terms = _evidence_terms(text)
    if "postgresql" in terms:
        terms.add("postgres")
    if any(term.endswith("vector") and term != "vector" for term in terms):
        terms.add("vector")
    if "deployment" in terms:
        terms.add("deploy")
    return terms


def _term_equivalents(term: str) -> set[str]:
    """Return approved concrete vocabulary alternatives for one term."""
    for family in _EVIDENCE_TERM_FAMILIES:
        if term in family:
            return set(family)
    return {term}


def _match_evidence_terms(query_terms: set[str], candidate_terms: set[str]) -> tuple[set[str], list[str]]:
    """Match query terms against candidate terms with auditable vocabulary use."""
    matched: set[str] = set()
    controlled: list[str] = []
    for term in query_terms:
        present = _term_equivalents(term) & candidate_terms
        if not present:
            continue
        matched.add(term)
        if term not in candidate_terms:
            controlled.append(f"{term}~{sorted(present)[0]}")
    return matched, controlled


def _unknown_claim_markers(clause: str, candidate_terms: set[str]) -> set[str]:
    """Return explicit names or identifiers absent from all retrieved records.

    An unknown capitalised name (``Zorblax``) or pasted identifier
    (``mcp-foreign-abc123``) makes a claim unanswerable even when the query also
    contains ordinary project words. Treating every long natural-language word
    as a marker was too aggressive: it rejected supported questions merely
    because the source used a synonym for words such as ``underneath`` or
    ``difference``. Ordinary language remains protected by direct-term matching
    below; this check is reserved for unmistakable claim anchors.
    """
    markers: set[str] = set()
    for raw in _EVIDENCE_TOKEN.findall(clause or ""):
        token = raw.strip("-./").lower()
        if token in _EVIDENCE_STOP_WORDS:
            continue
        # A hyphenated or numeric token is normally a pasted external marker,
        # so only its complete form can establish its presence. Underscores,
        # dots and paths are source notation: `memory_timeline` may be
        # documented as "timeline", so a distinctive component is sufficient.
        if "-" in raw or any(char.isdigit() for char in raw):
            markers.add(token)
        elif any(char in raw for char in "_./"):
            components = {
                component for component in _evidence_terms(raw)
                if _is_distinctive_term(component)
            }
            if components and not any(
                _term_equivalents(component) & candidate_terms
                for component in components
            ):
                markers.add(token)
        elif raw[0].isupper() and len(token) > 1:
            markers.add(token)
    return {
        marker for marker in markers
        if not (_term_equivalents(marker) & candidate_terms)
    }


def _evidence_clauses(query: str) -> list[tuple[str, set[str]]]:
    """Split joined questions without weakening the evidence bar for a claim."""
    clauses = [part.strip(" ,") for part in _COMPOUND_QUESTION.split(query) if part.strip(" ,")]
    return [(clause, _evidence_terms(clause)) for clause in clauses] or [(query, set())]


def _is_distinctive_term(term: str) -> bool:
    """A single precise identifier can be evidence; a single generic word is not."""
    return (term not in _GENERIC_LONG_TERMS
            and (len(term) >= 7 or "_" in term or "." in term or "/" in term
            or any(char.isdigit() for char in term))
    )


def select_evidence(
    query: str,
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep candidates that carry evidence for ``query`` and explain absence.

    RRF and feature ranking are intentionally relative: every non-empty corpus
    has a first result. Trust, recency, and utility also improve an ordering but
    cannot establish that the query is answered. This gate therefore uses only
    query-bound signals: direct terms and a resolved identifier/graph relation.
    A raw cross-encoder score is useful corroboration and ranking input, but it
    cannot establish evidence alone: a candidate sharing one real project term
    with an otherwise unrecorded claim can score highly. The floor is configured
    and must be calibrated by the retrieval evaluation suite as the corpus grows.
    """
    clauses = _evidence_clauses(query)
    selected: list[dict[str, Any]] = []
    rerank_floor = settings().evidence_rerank_min_score
    relationship_clauses = {
        index for index, (clause, _) in enumerate(clauses)
        if _RELATIONSHIP_QUERY.search(clause)
    }
    corpus_terms = {
        term for candidate in candidates
        for term in _candidate_evidence_terms(" ".join(str(candidate.get(field) or "")
                                                     for field in (
                                                         "title", "digest", "content", "identifiers",
                                                     )))
    }

    for candidate in candidates:
        searchable = " ".join(str(candidate.get(field) or "") for field in (
            "title", "digest", "content", "identifiers",
        ))
        candidate_terms = _candidate_evidence_terms(searchable)
        entity_signals: list[str] = []
        matched_terms: set[str] = set()
        matched_clause_indices: set[int] = set()
        signals: list[str] = []

        # Both arms begin with entities resolved from the query, so neither can
        # be produced merely because a recent document happened to rank highly.
        if candidate.get("r_ident") is not None:
            entity_signals.append("identifier")
        if candidate.get("r_graph") is not None:
            entity_signals.append("graph")

        cross_score = candidate.get("cross_score")
        for index, (clause, query_terms) in enumerate(clauses):
            matches, controlled_terms = _match_evidence_terms(query_terms, candidate_terms)
            unknown_markers = _unknown_claim_markers(clause, corpus_terms)
            # Generic project words remain useful for explanation, but cannot
            # make an unrelated claim look supported. The threshold is computed
            # from the terms that identify the claim itself.
            meaningful_terms = query_terms - _GENERIC_DIRECT_TERMS
            meaningful_matches = matches - _GENERIC_DIRECT_TERMS
            required_matches = (
                1 if len(meaningful_terms) == 1 else
                max(2, math.ceil(len(meaningful_terms) / 2))
            )
            direct_evidence = bool(required_matches and len(meaningful_matches) >= required_matches)
            if unknown_markers:
                continue
            if not direct_evidence:
                continue
            matched_terms.update(matches)
            matched_clause_indices.add(index)
            signals.append("direct_terms")
            if controlled_terms:
                signals.append("controlled_vocabulary")
            if cross_score is not None and float(cross_score) >= rerank_floor:
                signals.append("reranker")

        # A graph link shows that the candidate is near a named project entity;
        # it does not prove an arbitrary statement about that entity. It can
        # stand alone only for a relationship/impact question, otherwise it
        # corroborates direct evidence.
        if entity_signals and relationship_clauses:
            matched_clause_indices.update(relationship_clauses)
            signals = entity_signals + signals
        elif entity_signals and matched_clause_indices:
            signals = entity_signals + signals

        if not signals:
            continue
        selected.append({
            **candidate,
            "evidence": {
                "signals": list(dict.fromkeys(signals)),
                "matched_terms": sorted(matched_terms),
                "controlled_terms": sorted(set(controlled_terms)),
                "matched_clauses": [clauses[index][0] for index in sorted(matched_clause_indices)],
                "matched_clause_indices": sorted(matched_clause_indices),
            },
        })

    if selected:
        supported = {
            index for item in selected
            for index in item["evidence"]["matched_clause_indices"]
        }
        missing = [clause for index, (clause, _) in enumerate(clauses) if index not in supported]
        status = "supported" if not missing else "partial_support"
        return selected, {
            "status": status,
            "reason": (
                "Returned memories have direct relevance evidence; reranker scores are corroboration only."
                if status == "supported" else
                "Project memory supports part of this multi-part task; the remaining clause has no direct evidence."
            ),
            "considered_count": len(candidates),
            "evidence_count": len(selected),
            "supported_clauses": [clauses[index][0] for index in sorted(supported)],
            "missing_clauses": missing,
        }
    return [], {
        "status": "no_relevant_evidence",
        "reason": "No candidate had enough direct relevance evidence for this claim.",
        "considered_count": len(candidates),
        "evidence_count": 0,
        "supported_clauses": [],
        "missing_clauses": [clause for clause, _ in clauses],
    }


# Section headings that carry the ANSWER, in priority order. A structured
# document buries its conclusion below its motivation, and a blind truncation
# therefore captures the motivation and drops the conclusion.
#
# Measured, not assumed: ADR-0002's digest was its "## Context" paragraph —
# "Not all memory is produced in the same way..." — while the thing a reader
# actually needs, "Split memory into two planes", sat two sections lower and was
# never in the digest at all. That digest is what the pack shows an agent, what
# MMR dedups on, and what the cross-encoder scores, so the wrong 400 characters
# are wrong in three places at once.
PREFERRED_SECTIONS = (
    "decision", "resolution", "summary", "tl;dr", "tldr",
    "steps", "rules", "procedure",
)

_H2 = re.compile(r"^##+\s+(.+?)\s*$", re.M)


def split_sections(content: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) pairs. Prose before the first heading
    is returned under an empty heading."""
    marks = list(_H2.finditer(content))
    if not marks:
        return [("", content)]
    out: list[tuple[str, str]] = []
    if marks[0].start() > 0:
        out.append(("", content[: marks[0].start()]))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(content)
        out.append((m.group(1).strip().lower(), content[m.end():end]))
    return out


def _tidy(text: str, limit: int) -> str:
    """Flatten to prose and cut on a sentence boundary."""
    # Drop markdown list/emphasis noise: the digest is read as a sentence, and
    # "- **Plane A** — the ledger" reads worse than "Plane A — the ledger".
    flat = " ".join(
        re.sub(r"^[\s>*-]+|\*\*|`", "", line).strip()
        for line in text.splitlines() if line.strip()
    )
    flat = " ".join(flat.split())
    if len(flat) <= limit:
        return flat
    cut = flat[: limit - 1]
    stop = max(cut.rfind(". "), cut.rfind("; "))
    return (cut[: stop + 1] if stop > limit // 2 else cut.rstrip()) + "…"


def make_digest(title: str, content: str) -> str:
    """Digest-first retrieval means this is what most callers actually read.

    Prefers the section that states the conclusion, falling back to a plain
    truncation for unstructured content. Extractive and deterministic — Phase 5
    may replace the body with a generative summary, but not on the hot path
    (blueprint §5.4 rule 4: no LLM call at query time).
    """
    sections = split_sections(content)
    for want in PREFERRED_SECTIONS:
        for heading, body in sections:
            if heading == want or heading.startswith(want):
                d = _tidy(body, MAX_DIGEST - len(want) - 2)
                if d:
                    # Label it: an agent reading a pack should be able to tell a
                    # stated decision from background, and "Decision: ..." costs
                    # ten characters to say so.
                    return f"{want.capitalize()}: {d}"
    return _tidy(content, MAX_DIGEST)


def extract_identifiers(*parts: str) -> str:
    seen: dict[str, None] = {}
    for p in parts:
        for m in _IDENT.findall(p or ""):
            seen.setdefault(m, None)
    return " ".join(list(seen)[:200])


def assign_tier(source_type: str) -> str:
    return TIER_BY_SOURCE.get((source_type or "").lower(), "untrusted")


_INSERT = text("""
INSERT INTO mem.memories
  (tenant_id, memory_key, type, title, content, digest, scope_kind, project_id,
   owner_principal, tier, confidence, source_type, source_uri, source_version,
   asserted_by, status, token_cost, content_hash, identifiers, metadata)
VALUES
  (:tenant, :key, CAST(:mtype AS mem.memory_type), :title, :content, :digest,
   'project', :project, NULL, CAST(:tier AS mem.trust_tier), :confidence,
   :source_type, :source_uri, :source_version, :asserted_by,
   CAST(:status AS mem.memory_status), :token_cost, :hash, :identifiers,
   CAST(:metadata AS jsonb))
RETURNING id
""")


def write_memory(
    conn: Connection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    principal_id: UUID | None,
    mtype: str,
    title: str,
    content: str,
    source_type: str,
    memory_key: str | None = None,
    source_uri: str | None = None,
    source_version: str | None = None,
    metadata: dict[str, Any] | None = None,
    lifecycle: str | None = None,
) -> dict[str, Any]:
    """Write one memory inside an already-scoped transaction.

    Idempotent on (tenant_id, content_hash): re-ingesting unchanged content
    returns the existing row instead of creating a duplicate.

    ``lifecycle`` lets the SOURCE retire a document — an ADR whose frontmatter
    says ``status: superseded`` is knowledge the project has explicitly withdrawn,
    and retrieval filters on ``status = 'active'``. Without this the withdrawn
    document keeps being served as current, which is the exact failure a memory
    system exists to prevent. It can only retire a memory, never promote one:
    quarantine is a safety decision and a source file does not get to clear it.
    """
    if len(content) > MAX_CONTENT:
        raise ValueError(
            f"content is {len(content)} chars, limit {MAX_CONTENT}. Split it — an "
            "ADR is one memory and must not be chunked (05-BUILD-PLAN Phase 1)."
        )

    chash = content_hash(content)
    # Same keys on every path. An earlier version returned a short dict here and
    # the full one below, which forces every caller to branch on shape before it
    # can read `tier` — exactly the kind of thing the Phase 4 MCP adapter would
    # get wrong once and then carry.
    existing = conn.execute(
        text("SELECT m.id, m.digest, m.metadata, m.tier::text AS tier, "
             "       m.status::text AS status, "
             "       EXISTS (SELECT 1 FROM mem.memory_embeddings e "
             "               WHERE e.memory_id = m.id) AS embedded "
             "  FROM mem.memories m "
             " WHERE m.tenant_id = :t AND m.content_hash = :h LIMIT 1"),
        {"t": str(tenant_id), "h": chash},
    ).mappings().one_or_none()
    if existing:
        existing_meta = existing["metadata"] or {}
        # Content is the identity, but provenance is not. A rebase, squash or
        # cherry-pick rewrites the sha while leaving the bytes identical, so
        # deduplicating on content alone would pin the memory to a commit that no
        # longer exists in the branch — provenance that resolves to nothing is
        # worse than none, because it looks checkable.
        if source_version:
            conn.execute(
                text("UPDATE mem.memories SET source_version = :v, source_uri = :u "
                     " WHERE id = :i AND source_version IS DISTINCT FROM :v"),
                {"v": source_version, "u": source_uri, "i": str(existing["id"])},
            )
        # Same content can still yield a BETTER digest, because the digest is
        # derived by code that changes. Content identity is the dedup key; the
        # digest is a derived view of it, and pinning derived data to whatever
        # algorithm happened to run first means an improvement never reaches
        # anything already ingested — the corpus silently splits into old-style
        # and new-style digests with no way to tell which is which.
        fresh = make_digest(title, content)
        if fresh != existing["digest"]:
            conn.execute(
                text("UPDATE mem.memories SET digest = :d WHERE id = :i"),
                {"d": fresh, "i": str(existing["id"])},
            )
            _refresh_digest_embedding(conn, tenant_id, existing["id"], fresh)

        # Retiring a document usually reaches HERE, not the create path below.
        # Ingest strips frontmatter before hashing, so editing `status: accepted`
        # to `status: superseded` changes not one byte of content: the hash is
        # unchanged, dedup matches, and handling lifecycle only on the create path
        # would mean withdrawing a decision silently did nothing at all.
        effective_status = existing["status"]
        if (lifecycle and lifecycle != effective_status
                and effective_status != "quarantined"):
            conn.execute(
                text("UPDATE mem.memories "
                     "   SET status = CAST(:s AS mem.memory_status), "
                     "       superseded_at = CASE WHEN :s = 'active' THEN NULL "
                     "                            ELSE now() END "
                     " WHERE id = :i"),
                {"s": lifecycle, "i": str(existing["id"])},
            )
            effective_status = lifecycle
        return {
            "id": existing["id"], "created": False, "deduplicated": True,
            "tier": existing["tier"], "status": effective_status,
            "embedded": existing["embedded"], "superseded": None,
            "digest_refreshed": fresh != existing["digest"],
            # Same keys on every path. Adding a field to the create path only
            # (which is what happened when injection scanning landed) makes the
            # second call to a deduplicating writer return a different shape than
            # the first — a bug that only shows up on re-run.
            "injection_flagged": bool((existing_meta or {}).get("injection")),
        }

    tier = assign_tier(source_type)

    # Indirect prompt injection (Suite 5). Unreviewed content carrying agent
    # directives is capped at `untrusted` and quarantined; reviewed Plane A
    # content is flagged only, because human review is the control for that
    # plane and quarantining it would delete a project's own writing about
    # prompt injection from its own memory.
    from . import injection as _injection
    verdict = _injection.assess(f"{title}\n{content}", source_type)
    if verdict["tier_cap"]:
        tier = verdict["tier_cap"]

    status = "quarantined" if (tier in QUARANTINE_TIERS or verdict["quarantine"]) else "active"
    # A source may retire its own document, but may not un-quarantine one: the
    # quarantine decision above is a safety control and the file being ingested is
    # exactly the thing it is protecting against.
    if lifecycle and status != "quarantined":
        status = lifecycle
    digest = make_digest(title, content)

    # Same key, different content: this is an EDIT, and under the bi-temporal
    # model (ADR-0006) the previous version has to be closed before the new one
    # opens. mem.memories_temporal_uniq is UNIQUE (tenant_id, memory_key,
    # valid_at WITHOUT OVERLAPS), so skipping this does not corrupt anything — it
    # raises ExclusionViolation and the edit is simply rejected, which for an
    # ADR being revised is the single most common ingestion case there is.
    superseded_id = None
    if memory_key:
        # Deliberately NOT filtered by status. memories_temporal_uniq constrains
        # (tenant_id, memory_key, valid_at WITHOUT OVERLAPS) and knows nothing
        # about status, so an archived-but-still-open row collides just as hard as
        # an active one. Filtering on status='active' here means deleting a file
        # and later restoring it fails forever with ExclusionViolation.
        prior = conn.execute(
            text("SELECT id FROM mem.memories "
                 " WHERE tenant_id = :t AND memory_key = :k "
                 "   AND upper(valid_at) IS NULL "
                 " ORDER BY recorded_at DESC LIMIT 1"),
            {"t": str(tenant_id), "k": memory_key},
        ).scalar_one_or_none()
        if prior:
            # Close the old row's validity at now() rather than deleting it: "what
            # did we believe in June" has to stay answerable.
            conn.execute(
                text("UPDATE mem.memories "
                     "   SET valid_at = tstzrange(lower(valid_at), now(), '[)'), "
                     "       status = 'superseded', superseded_at = now() "
                     " WHERE id = :i"),
                {"i": str(prior)},
            )
            superseded_id = prior

    mid = conn.execute(_INSERT, {
        "tenant": str(tenant_id),
        "key": memory_key or f"{source_type}:{chash[:16]}",
        "mtype": mtype,
        "title": title[:200],
        "content": content,
        "digest": digest,
        "project": str(project_id),
        "tier": tier,
        "confidence": CONFIDENCE_BY_TIER[tier],
        "source_type": source_type,
        "source_uri": source_uri,
        "source_version": source_version,
        "asserted_by": str(principal_id) if principal_id else None,
        "status": status,
        "token_cost": count_tokens(content),
        "hash": chash,
        "identifiers": extract_identifiers(title, content),
        # The signals travel with the memory: the review inbox has to show WHY
        # something was quarantined, and "trust me" is not an audit trail.
        "metadata": json.dumps({
            **(metadata or {}),
            **({"injection": verdict["signals"]} if verdict["flagged"] else {}),
        }),
    }).scalar_one()

    if superseded_id:
        # The edge, not just the two rows: memory.explain has to be able to walk
        # from a current memory back through what it replaced.
        conn.execute(
            text("INSERT INTO mem.memory_supersessions "
                 "  (tenant_id, new_id, old_id, reason) "
                 "VALUES (:t, :new, :old, :reason) ON CONFLICT DO NOTHING"),
            {"t": str(tenant_id), "new": str(mid), "old": str(superseded_id),
             "reason": f"content changed at source ({source_type})"},
        )

    # Link entities so the graph arm has something to walk. Failure here must not
    # lose the memory: extraction is an enrichment, and a memory with no entity
    # links is merely invisible to one of five arms.
    try:
        from . import entities as _entities

        # Authored glossary terms must be matchable on every write, not only
        # after a restart. The glossary UPSERT itself lives in ingest.py: it has
        # to run even when write_memory dedupes an unchanged document.
        _entities.load_authored(conn, tenant_id=tenant_id, project_id=project_id)

        _entities.link_memory(conn, tenant_id=tenant_id, project_id=project_id,
                              memory_id=mid, title=title, content=content)
        _entities.link_relations(conn, tenant_id=tenant_id, project_id=project_id,
                                 memory_id=mid, title=title, content=content,
                                 source_type=source_type, metadata=metadata)
    except Exception as exc:  # noqa: BLE001
        log.warning("entity linking failed for %s (memory kept): %s", mid, exc)

    embedded = _embed_memory(conn, tenant_id, mid, title, digest, content)
    return {
        "id": mid, "created": True, "deduplicated": False,
        "tier": tier, "status": status, "embedded": embedded,
        "superseded": str(superseded_id) if superseded_id else None,
        "injection_flagged": verdict["flagged"],
    }


def _refresh_digest_embedding(conn, tenant_id: UUID, mid, digest: str) -> None:
    """Keep digest_embedding consistent with the digest it is derived from.

    MMR dedup and the cross-encoder both work off the digest, so a refreshed
    digest with a stale vector would make near-duplicate detection disagree with
    what the pack actually shows. Failure here is tolerated (ADR-0008): a stale
    vector degrades dedup quality, it does not lose the memory.
    """
    try:
        vec = embeddings.embed_one(digest)
    except embeddings.EmbeddingUnavailable as exc:
        log.warning("digest re-embed skipped for %s: %s", mid, exc)
        return
    conn.execute(
        text("UPDATE mem.memory_embeddings SET digest_embedding = CAST(:d AS halfvec(1024)) "
             " WHERE memory_id = :i AND tenant_id = :t"),
        {"d": embeddings.to_pgvector(vec), "i": str(mid), "t": str(tenant_id)},
    )


def _embed_memory(conn, tenant_id: UUID, mid, title: str, digest: str, content: str) -> bool:
    """Synchronous embed on write (05-BUILD-PLAN Phase 1).

    A failure here does NOT fail the write. ADR-0008 has retrieval degrade to the
    lexical arm rather than fail closed, and the same reasoning applies on the way
    in: a memory with no vector is still findable by text and can be backfilled,
    whereas a rejected write is simply lost. The row is left without an embedding
    and logged loudly.
    """
    try:
        model_id = embeddings.ensure_registered(conn)
        vecs = embeddings.provider().embed([f"{title}\n\n{content}", digest])
    except embeddings.EmbeddingUnavailable as exc:
        log.warning("embedding unavailable for %s, stored without vector: %s", mid, exc)
        return False

    conn.execute(
        text("INSERT INTO mem.memory_embeddings "
             "  (memory_id, model_id, tenant_id, embedding, digest_embedding) "
             "VALUES (:m, :mo, :t, CAST(:v AS halfvec(1024)), CAST(:d AS halfvec(1024)))"),
        {"m": str(mid), "mo": model_id, "t": str(tenant_id),
         "v": embeddings.to_pgvector(vecs[0]), "d": embeddings.to_pgvector(vecs[1])},
    )
    return True


def reembed_memory(conn: Connection, *, tenant_id: UUID, memory_id: UUID) -> dict[str, Any]:
    """Refresh one memory in the active embedding model.

    A console request is an explicit operator action, not the background
    backfill. It therefore replaces the vector for the active model even when a
    row already exists. Older model rows stay intact: deleting them would erase
    the evidence needed to compare vector spaces during a model migration.
    """
    row = conn.execute(
        text("SELECT id, title, content, digest FROM mem.memories "
             "WHERE id = :id AND tenant_id = :tenant"),
        {"id": str(memory_id), "tenant": str(tenant_id)},
    ).mappings().one_or_none()
    if row is None:
        raise LookupError("no such memory in this scope")

    model_id = embeddings.ensure_registered(conn)
    try:
        vectors = embeddings.provider().embed([
            f"{row['title']}\n\n{row['content']}", row["digest"],
        ])
    except embeddings.EmbeddingUnavailable as exc:
        raise ValueError(f"embedding provider is unavailable: {exc}") from exc

    conn.execute(
        text("INSERT INTO mem.memory_embeddings "
             "  (memory_id, model_id, tenant_id, embedding, digest_embedding) "
             "VALUES (:memory, :model, :tenant, CAST(:embedding AS halfvec(1024)), "
             "        CAST(:digest AS halfvec(1024))) "
             "ON CONFLICT (memory_id, model_id) DO UPDATE "
             "SET embedding = EXCLUDED.embedding, "
             "    digest_embedding = EXCLUDED.digest_embedding, created_at = now()"),
        {"memory": str(memory_id), "model": model_id, "tenant": str(tenant_id),
         "embedding": embeddings.to_pgvector(vectors[0]),
         "digest": embeddings.to_pgvector(vectors[1])},
    )
    return {"id": str(memory_id), "model_id": model_id, "embedded": True}


_LEXICAL = text("""
SELECT m.id, m.title, m.digest, m.content, m.tier::text, m.type::text,
       ts_rank_cd(m.content_tsv, {q}) AS score
 FROM mem.memories m
 WHERE m.content_tsv @@ {q}
   AND m.valid_at @> CAST(:as_of AS timestamptz)
   AND m.recorded_at <= CAST(:as_of AS timestamptz)
   AND (:historical OR m.status = 'active')
 ORDER BY score DESC
 LIMIT :k
""".format(q="websearch_to_tsquery('english', :q)"))


def _lexical_only(
    conn: Connection,
    query: str,
    limit: int,
    *,
    as_of: datetime | None = None,
    historical: bool = False,
) -> list[dict[str, Any]]:
    """The lexical arm standing alone, when the vector arm is unavailable.

    Uses websearch_to_tsquery to match mem.search_hybrid's own lexical arm — using
    plainto_tsquery here instead would make degraded results differ from healthy
    results for reasons unrelated to the outage.

    Then it widens. websearch_to_tsquery ANDs every term, which is the right
    default when it is one arm of five: the vector arm covers the paraphrases it
    misses. As the ONLY surviving arm that strictness turns an outage into "no
    results", which reads to a caller as "nothing was ever written". So if the
    strict pass finds nothing we retry OR-ing the terms and let ts_rank_cd sort
    it out — recall matters more than precision when the alternative is silence.
    """
    effective_as_of = as_of or datetime.now(timezone.utc)
    rows = conn.execute(_LEXICAL, {
        "q": query, "k": limit, "as_of": effective_as_of,
        "historical": historical,
    }).mappings().all()
    if rows:
        return [dict(r, degraded=True, lexical_mode="all-terms") for r in rows]

    terms = [t for t in re.findall(r"[A-Za-z0-9_]+", query) if len(t) > 2]
    if not terms:
        return []
    rows = conn.execute(
        text("""
        SELECT m.id, m.title, m.digest, m.content, m.tier::text, m.type::text,
               ts_rank_cd(m.content_tsv, to_tsquery('english', :q)) AS score
         FROM mem.memories m
         WHERE m.content_tsv @@ to_tsquery('english', :q)
           AND m.valid_at @> CAST(:as_of AS timestamptz)
           AND m.recorded_at <= CAST(:as_of AS timestamptz)
           AND (:historical OR m.status = 'active')
         ORDER BY score DESC
         LIMIT :k
        """),
        {"q": " | ".join(terms), "k": limit, "as_of": effective_as_of,
         "historical": historical},
    ).mappings().all()
    return [dict(r, degraded=True, lexical_mode="any-term") for r in rows]


def search(
    conn: Connection,
    query: str,
    *,
    limit: int = 8,
    min_tier: str = "observed",
    tenant_id: UUID | None = None,
    project_id: UUID | None = None,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    """Hybrid retrieval. Falls back to the lexical arm alone if the embedder is
    down, rather than returning nothing (ADR-0008)."""
    try:
        qvec = embeddings.to_pgvector(embeddings.embed_one(query))
        degraded = False
    except embeddings.EmbeddingUnavailable as exc:
        log.warning("embedder down, lexical-only retrieval: %s", exc)
        qvec, degraded = None, True

    effective_as_of = as_of or datetime.now(timezone.utc)
    if degraded:
        return _lexical_only(conn, query, limit, as_of=effective_as_of,
                             historical=as_of is not None)

    # Over-fetch, then rerank. Two reasons this is not just RRF order:
    #
    #  * memory_search and memory_context must agree. They are the same product
    #    surface; if one orders by raw RRF and the other by the feature model,
    #    an agent that searches and then asks for context gets two different
    #    answers to the same question and neither is explainable.
    #  * RRF alone ignores trust. The reviewed ADR and the unverified scrap fuse
    #    identically, which is the whole reason the trust lattice exists.
    # The graph arm walks out from entities named in the QUERY. Passing NULL here
    # (the previous behaviour) makes the arm a no-op on every search, which is
    # half the reason it measured 0%.
    from . import entities as _entities
    ent = _entities.resolve_query_entities(conn, query, tenant_id=tenant_id,
                                           project_id=project_id) if tenant_id else []

    rows = conn.execute(text("""
        SELECT m.id, m.title, m.digest, m.content, m.tier::text AS tier, m.type::text AS type,
               m.importance_prior, m.utility, m.retrieval_count, m.recorded_at,
               m.identifiers, m.status::text AS status,
               s.rrf_score, s.r_vec, s.r_lex, s.r_ident, s.r_graph, s.r_time
          FROM mem.search_hybrid(CAST(:v AS halfvec(1024)), :q, NULL,
                                 CAST(:tier AS mem.trust_tier), CAST(:as_of AS timestamptz),
                                 CAST(:entity_ids AS uuid[]), :k, 60,
                                 CAST(:statuses AS mem.memory_status[])) s
          JOIN mem.memories m ON m.id = s.memory_id
         WHERE m.valid_at @> CAST(:as_of AS timestamptz)
           AND m.recorded_at <= CAST(:as_of AS timestamptz)
    """), {"v": qvec, "q": query, "tier": min_tier,
           "entity_ids": "{" + ",".join(ent) + "}",
           "k": max(limit * 4, 40), "as_of": effective_as_of,
           "statuses": "{" + ",".join(
               ["active", "archived", "superseded"] if as_of is not None else ["active"]
           ) + "}"}).mappings().all()

    # Local imports: ranking/planner must not depend on memories (context.py
    # imports all three, and a cycle here would surface as an import error only
    # on the worker, which loads modules in a different order).
    from . import planner, ranking, reranker
    _, weights = ranking.load_profile(conn)
    qplan = planner.plan(query)
    cands, rr_meta = reranker.apply(query, [dict(r) for r in rows])
    # Order matters: cross-encoder first, feature model second. The blueprint puts
    # it "between RRF and the feature model" so trust and recency still get the
    # final say — a cross-encoder has no idea which of two equally relevant
    # memories was human-reviewed.
    ranked = ranking.rerank(
        cands, weights=weights, plan=qplan,
        task_terms=set(re.findall(r"[A-Za-z_][A-Za-z0-9_.]{2,}", query)))
    return [dict(r, degraded=False, intent=qplan.intent, rerank=rr_meta)
            for r in ranked[:limit]]
