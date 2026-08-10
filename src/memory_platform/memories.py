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
})
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
        token = raw.lower()
        if len(token) <= 1 or token in _EVIDENCE_STOP_WORDS:
            continue
        if token.endswith("ies") and len(token) > 4:
            token = token[:-3] + "y"
        elif (token.endswith("s") and len(token) > 4
              and token not in {"postgres", "status"} and not token.endswith("ss")):
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


def _evidence_clauses(query: str) -> list[tuple[str, set[str]]]:
    """Split joined questions without weakening the evidence bar for a claim."""
    clauses = [part.strip(" ,") for part in _COMPOUND_QUESTION.split(query) if part.strip(" ,")]
    return [(clause, _evidence_terms(clause)) for clause in clauses] or [(query, set())]


def _is_distinctive_term(term: str) -> bool:
    """A single precise identifier can be evidence; a single generic word is not."""
    return (len(term) >= 7 or "_" in term or "." in term or "/" in term
            or any(char.isdigit() for char in term))


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
        for index, (_, query_terms) in enumerate(clauses):
            matches = query_terms & candidate_terms
            required_matches = (
                1 if len(query_terms) == 1 else
                max(2, math.ceil(len(query_terms) / 2))
            )
            direct_evidence = bool(required_matches and len(matches) >= required_matches)
            lexical_evidence = bool(
                matches and candidate.get("r_lex") is not None
                and any(_is_distinctive_term(term) for term in matches)
                and len(query_terms) <= 2
            )
            if not (direct_evidence or lexical_evidence):
                continue
            matched_terms.update(matches)
            matched_clause_indices.add(index)
            signals.append("direct_terms" if direct_evidence else "lexical")
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
) -> dict[str, Any]:
    """Write one memory inside an already-scoped transaction.

    Idempotent on (tenant_id, content_hash): re-ingesting unchanged content
    returns the existing row instead of creating a duplicate.
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
        return {
            "id": existing["id"], "created": False, "deduplicated": True,
            "tier": existing["tier"], "status": existing["status"],
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
