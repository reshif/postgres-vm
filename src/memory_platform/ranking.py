"""Feature reranking and MMR dedup — stage 2 of retrieval.

RRF (in mem.search_hybrid) fuses the arms without needing score calibration
between them. This module then reranks on features of the memory itself, per
00-MASTER-BLUEPRINT.md §575:

    final = a*rrf_norm + b*trust + c*importance + d*utility
          + e*recency + f*entity_overlap - g*redundancy

Weights are loaded from mem.ranking_profiles, never hard-coded here. The
blueprint makes changing a weight a deployment gated by the eval suite, which is
only enforceable if the weights are data that a retrieval_event can point at.

Every component score is returned alongside the total. That is not debug
instrumentation — `memory.explain` is a product surface (ADR-0003 calls it "the
trust surface"), and it can only explain a ranking if the ranking recorded why.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

log = logging.getLogger("memory.ranking")

# Bumped by migration 0009. load_profile() falls back to any active row, so a
# deployment mid-migration degrades to the previous profile rather than raising.
DEFAULT_PROFILE = "default@2"


def load_profile(conn: Connection, profile_id: str = DEFAULT_PROFILE) -> tuple[str, dict]:
    """Load a ranking profile, falling back to any active one.

    Raises if none exists: silently reranking with implicit weights would produce
    an ordering that no stored retrieval_event could ever reproduce.
    """
    row = conn.execute(
        text("SELECT id, weights FROM mem.ranking_profiles "
             " WHERE id = :i AND active ORDER BY created_at DESC LIMIT 1"),
        {"i": profile_id},
    ).mappings().one_or_none()
    if row is None:
        row = conn.execute(
            text("SELECT id, weights FROM mem.ranking_profiles "
                 " WHERE active ORDER BY created_at DESC LIMIT 1")
        ).mappings().one_or_none()
    if row is None:
        raise RuntimeError(
            "no active row in mem.ranking_profiles. Migrations 0007/0009 seed "
            "'default@1'/'default@2'; run `alembic upgrade head`."
        )
    return row["id"], dict(row["weights"])


def recency_score(mtype: str, recorded_at: datetime | None, weights: dict) -> float:
    """Exponential decay with a per-type half-life.

    A single global half-life is the tempting simplification and it is wrong in
    both directions at once: fast enough to keep episodes fresh means decisions
    fade out of retrieval within a year, and slow enough to preserve decisions
    means last month's incident keeps outranking this week's.
    """
    if recorded_at is None:
        return 0.5
    half_lives = weights.get("recency_half_life_days", {})
    hl = float(half_lives.get(mtype, 180))
    now = datetime.now(timezone.utc)
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - recorded_at).total_seconds() / 86400.0)
    return 0.5 ** (age_days / hl) if hl > 0 else 0.5


def entity_overlap(identifiers: str, task_terms: set[str]) -> float:
    """Fraction of the task's identifier-like terms present in the memory.

    Cheap and deliberately literal: this is the arm that catches "the task
    mentions HttpClient and so does this memory", which embeddings routinely
    miss because a rare symbol contributes little to a sentence vector.
    """
    if not task_terms or not identifiers:
        return 0.0
    have = {t.lower() for t in identifiers.split()}
    hit = sum(1 for t in task_terms if t.lower() in have)
    return hit / len(task_terms)


def _pairwise_cosines(
    ranked: list[dict[str, Any]], vectors: dict[Any, list[float]],
) -> dict[tuple[int, int], float]:
    """Calculate each candidate-pair similarity once for MMR.

    The original MMR loop recomputed the same digest cosine every time another
    candidate was selected. For a 60-candidate pack that is tens of thousands of
    1024-dimensional dot products, even though only 1,770 distinct pairs exist.
    The cache keeps MMR's selection and duplicate threshold exactly the same; it
    removes only repeated arithmetic from the hot path.
    """
    prepared: list[tuple[list[float], float]] = []
    for candidate in ranked:
        vector = vectors.get(candidate["id"], [])
        norm = math.sqrt(sum(value * value for value in vector)) if vector else 0.0
        prepared.append((vector, norm))

    similarities: dict[tuple[int, int], float] = {}
    for left, (left_vector, left_norm) in enumerate(prepared):
        if not left_norm:
            continue
        for right in range(left + 1, len(prepared)):
            right_vector, right_norm = prepared[right]
            if not right_norm or len(left_vector) != len(right_vector):
                continue
            similarities[(left, right)] = (
                sum(a * b for a, b in zip(left_vector, right_vector)) /
                (left_norm * right_norm)
            )
    return similarities


def rerank(
    candidates: list[dict[str, Any]],
    *,
    weights: dict,
    task_terms: set[str] | None = None,
    plan: Any | None = None,
) -> list[dict[str, Any]]:
    """Score candidates and return them ordered, each carrying its decomposition.

    `candidates` come from mem.search_hybrid joined to mem.memories, so each has
    rrf_score, tier, importance_prior, utility, retrieval_count, type,
    recorded_at and identifiers.
    """
    if not candidates:
        return []

    task_terms = task_terms or set()
    trust_w = weights.get("trust_weights", {})
    min_retr = int(weights.get("utility_min_retrievals", 5))
    # Types the stage-1 planner expects to carry the answer. A bias, never a
    # filter: see the note at the top of planner.py — a hard type filter turns
    # every misclassification into an unrecoverable miss.
    primary = set(getattr(plan, "memory_types", ()) or ())

    # Normalise RRF within this result set. RRF values depend on set size and
    # k, so an absolute value is not comparable across queries — only the
    # ordering within one result set is meaningful.
    rrfs = [float(c.get("rrf_score") or 0.0) for c in candidates]
    lo, hi = min(rrfs), max(rrfs)
    span = (hi - lo) or 1.0

    # When a cross-encoder has scored these candidates, its score REPLACES
    # rrf_norm as the query-relevance term rather than being added alongside it.
    #
    # Both measure the same thing — how well this memory answers this query — and
    # the cross-encoder measures it better, because it reads the query and the
    # document together instead of comparing two independently-produced vectors.
    # Adding a separate term would double-count relevance and dilute trust and
    # recency; leaving it out entirely (the first version of this code) meant the
    # feature model re-sorted by rrf_score and threw the cross-encoder's ordering
    # away, so the reranker ran, cost latency, and changed nothing.
    #
    # Substituting keeps the profile weights meaningful and unchanged: with the
    # reranker off, every score is identical to before.
    xs = [c.get("cross_score") for c in candidates]
    use_cross = any(x is not None for x in xs)
    if use_cross:
        vals = [float(x or 0.0) for x in xs]
        xlo, xhi = min(vals), max(vals)
        xspan = (xhi - xlo) or 1.0

    scored = []
    for c in candidates:
        rrf_norm = (float(c.get("rrf_score") or 0.0) - lo) / span
        relevance, relevance_from = rrf_norm, "rrf"
        if use_cross:
            relevance = (float(c.get("cross_score") or 0.0) - xlo) / xspan
            relevance_from = "cross_encoder"
        trust = float(trust_w.get(c.get("tier"), 0.1))
        importance = float(c.get("importance_prior") or 0.5)

        # Cold-start guard (ADR-0009): a memory nobody has retrieved yet has no
        # measured utility. Scoring its 0.0 utility as if it were a measurement
        # buries new knowledge under old knowledge permanently.
        retrievals = int(c.get("retrieval_count") or 0)
        utility = float(c.get("utility") or 0.0) if retrievals >= min_retr else 0.0
        utility_applied = retrievals >= min_retr

        recency = recency_score(c.get("type", ""), c.get("recorded_at"), weights)
        overlap = entity_overlap(c.get("identifiers") or "", task_terms)
        intent_match = 1.0 if (primary and c.get("type") in primary) else 0.0

        parts = {
            "rrf": weights.get("rrf", 0.0) * relevance,
            "trust": weights.get("trust", 0.0) * trust,
            "importance": weights.get("importance", 0.0) * importance,
            "utility": weights.get("utility", 0.0) * utility,
            "recency": weights.get("recency", 0.0) * recency,
            "entity_overlap": weights.get("entity_overlap", 0.0) * overlap,
            "intent_match": weights.get("intent_match", 0.0) * intent_match,
        }
        scored.append({
            **c,
            "score": sum(parts.values()),
            "score_parts": {k: round(v, 5) for k, v in parts.items()},
            "score_inputs": {
                "rrf_norm": round(rrf_norm, 5),
                "relevance": round(relevance, 5),
                "relevance_from": relevance_from,
                "cross_score": c.get("cross_score"),
                "trust": trust,
                "importance": importance, "utility": utility,
                "utility_applied": utility_applied, "recency": round(recency, 5),
                "entity_overlap": round(overlap, 5),
                "intent_match": intent_match,
                "intent": getattr(plan, "intent", None),
            },
        })

    # Ties MUST break deterministically, and on something meaningful.
    #
    # Sorting on `score` alone leaves ties to Python's stable sort, which
    # preserves whatever order the database happened to return — and the
    # candidate query has no ORDER BY, so that order is arbitrary. On a corpus
    # where most feature values are identical (every memory the same tier, type
    # and age, which is exactly what a freshly ingested .memory/ tree looks
    # like), score ties are the common case rather than the edge case, so the
    # arbitrary order effectively *becomes* the ranking. Measured on the golden
    # set, that alone moved recall@5 by 8.6 points.
    #
    # Falling back to rrf_score keeps the query-derived signal as the
    # tie-breaker, then id as a final total order so the same corpus ranks
    # identically on every machine and every run — without which a stored
    # retrieval_event cannot be reproduced, and the Retrieval Debugger is
    # explaining an ordering that no longer exists.
    scored.sort(
        key=lambda r: (r["score"], float(r.get("rrf_score") or 0.0), str(r["id"])),
        reverse=True,
    )
    return scored


def mmr_dedup(
    ranked: list[dict[str, Any]],
    *,
    weights: dict,
    vectors: dict[Any, list[float]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Maximal Marginal Relevance over digest embeddings.

    Returns (kept, dropped). Dropped items are returned rather than discarded
    because the Retrieval Debugger has to explain "every returned AND dropped
    item" (05-BUILD-PLAN Phase 3 acceptance) — a pack that silently omits three
    near-duplicates is indistinguishable from one that never found them.

    Near-duplicates above dedup_cosine collapse into the highest-scoring member,
    which accumulates the rest as `also_seen_in` (blueprint §5.3).
    """
    if not ranked:
        return [], []

    vectors = vectors or {}
    lam = float(weights.get("mmr_lambda", 0.7))
    dup_at = float(weights.get("dedup_cosine", 0.94))
    original_index = {id(candidate): index for index, candidate in enumerate(ranked)}
    similarities = _pairwise_cosines(ranked, vectors)

    def similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
        left_index = original_index[id(left)]
        right_index = original_index[id(right)]
        if left_index > right_index:
            left_index, right_index = right_index, left_index
        return similarities.get((left_index, right_index), 0.0)

    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    pool = list(ranked)

    while pool:
        best_i, best_val = 0, None
        for i, cand in enumerate(pool):
            sim = max((similarity(cand, prior) for prior in kept), default=0.0)
            val = lam * cand["score"] - (1 - lam) * sim
            if best_val is None or val > best_val:
                best_i, best_val = i, val

        chosen = pool.pop(best_i)

        collapsed_into = None
        for k in kept:
            if similarity(chosen, k) >= dup_at:
                collapsed_into = k
                break
        if collapsed_into is not None:
            collapsed_into.setdefault("also_seen_in", []).append(str(chosen["id"]))
            chosen["dropped_reason"] = (
                f"near-duplicate of {collapsed_into['id']} "
                f"(cosine >= {dup_at} on digest)"
            )
            dropped.append(chosen)
        else:
            chosen["mmr_value"] = round(best_val, 5) if best_val is not None else None
            kept.append(chosen)

    return kept, dropped
