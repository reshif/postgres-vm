"""Consolidation — dedup and episode compaction (00-MASTER-BLUEPRINT §6.5).

Two nightly passes, both idempotent, both writing an auditable
`mem.consolidation_runs` row:

  1. Dedup — collapse near-identical memories, preserving the highest trust and
     merging provenance.
  2. Episode compaction — >= N similar episodes older than M days become one
     summary memory, with the originals archived rather than deleted.

WHAT THIS IS ALLOWED TO TOUCH, AND WHY THE LIMITS ARE NOT NEGOTIABLE.

Consolidation is the only unattended process that removes memories from
retrieval without a human asking it to. Two rules follow from that, and both are
enforced here rather than left to the caller:

**Plane A is never consolidated.** Documents ingested from git (ADR-0002) are the
reviewed record. Collapsing two ADRs because they embed similarly would put the
database out of agreement with the repository, and the next ingest would restore
the row anyway — so the pass would flap forever while producing audit rows
claiming work. Plane A is deduplicated at its source, by review.

**Nothing is deleted.** Collapsed and compacted memories are archived with their
`valid_at` closed, exactly as decay does. "What did we believe in June" has to
stay answerable (ADR-0006), and the console has to be able to walk from a summary
back to the episodes it replaced — which is why supersession is recorded as a
`mem.memory_supersessions` edge rather than a status change alone.

THRESHOLDS ARE CONFIGURATION, NOT CONSTANTS. The blueprint's ">= 20 episodes
older than 30 days" is a reasonable default for a busy project and completely
wrong for a new one, where it means compaction never runs at all and the feature
silently does not exist. Every threshold is a setting, recorded in the audit row
so a run from last month can still be interpreted.

SIMILARITY THRESHOLDS DIFFER BY PASS, DELIBERATELY. Dedup runs at a much tighter
cosine than the MMR dedup in retrieval (default 0.94). MMR only hides a result
from one response and is reversible on the next query; this archives a row. The
cost of a false positive is not symmetric, so the threshold is not either.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .config import settings

log = logging.getLogger("memory.consolidation")

# Only Plane B, and only types where near-duplicates are a real phenomenon.
# Decisions and conventions are excluded even in Plane B: two similarly worded
# constraints are usually two constraints.
DEDUP_TYPES = ("episode", "observation", "failure", "success", "session_summary")
COMPACT_TYPES = ("episode", "observation")

# Trust ordering for choosing a survivor. Higher wins.
TIER_RANK = {"untrusted": 0, "inferred": 1, "observed": 2,
             "verified": 3, "authoritative": 4}


def _open_run(conn: Connection, *, tenant_id: UUID, project_id: UUID,
              kind: str, parameters: dict[str, Any]) -> UUID:
    return conn.execute(
        text("INSERT INTO mem.consolidation_runs "
             "  (tenant_id, project_id, kind, parameters) "
             "VALUES (:t, :p, :k, CAST(:params AS jsonb)) RETURNING id"),
        {"t": str(tenant_id), "p": str(project_id), "k": kind,
         "params": json.dumps(parameters)},
    ).scalar_one()


def _close_run(conn: Connection, run_id: UUID, *, examined: int, affected: int,
               details: dict[str, Any], status: str = "completed",
               error: str | None = None) -> None:
    conn.execute(
        text("UPDATE mem.consolidation_runs "
             "   SET status = :s, examined = :e, affected = :a, "
             "       details = CAST(:d AS jsonb), error = :err, "
             "       completed_at = now() "
             " WHERE id = :i"),
        {"s": status, "e": examined, "a": affected,
         "d": json.dumps(details)[:200000], "err": error, "i": str(run_id)},
    )


def _archive(conn: Connection, memory_id: UUID, survivor_id: UUID,
             reason: str, tenant_id: UUID) -> None:
    """Archive a memory in favour of a survivor, recording the edge.

    Closing valid_at as well as setting the status is what makes the row stop
    competing for retrieval while remaining reachable through an as_of query.
    Status alone would leave an open interval, and the temporal uniqueness
    constraint treats an archived-but-open row as live.
    """
    conn.execute(
        text("UPDATE mem.memories "
             "   SET status = 'archived', superseded_at = now(), "
             "       valid_at = tstzrange(lower(valid_at), now(), '[)') "
             " WHERE id = :i AND upper(valid_at) IS NULL"),
        {"i": str(memory_id)},
    )
    conn.execute(
        text("INSERT INTO mem.memory_supersessions (tenant_id, new_id, old_id, reason) "
             "VALUES (:t, :new, :old, :reason) ON CONFLICT DO NOTHING"),
        {"t": str(tenant_id), "new": str(survivor_id), "old": str(memory_id),
         "reason": reason},
    )


def _candidates(conn: Connection, *, tenant_id: UUID, project_id: UUID,
                types: tuple[str, ...], older_than_days: int | None = None,
                limit: int = 2000) -> list[dict[str, Any]]:
    """Active, embedded, unpinned Plane B memories eligible for a pass."""
    clauses = [
        "m.tenant_id = :t", "m.project_id = :p", "m.status = 'active'",
        "upper(m.valid_at) IS NULL", "NOT m.pinned",
        "m.type::text = ANY(:types)",
        # See the module docstring: git-sourced content is Plane A.
        "m.source_type <> 'git'",
        "e.embedding IS NOT NULL",
    ]
    params: dict[str, Any] = {"t": str(tenant_id), "p": str(project_id),
                              "types": list(types), "limit": limit}
    if older_than_days is not None:
        clauses.append("m.recorded_at < now() - make_interval(days => :days)")
        params["days"] = older_than_days

    return [dict(r) for r in conn.execute(
        text("SELECT m.id, m.title, m.digest, m.content, m.tier::text AS tier, "
             "       m.type::text AS type, m.recorded_at, m.retrieval_count, "
             "       m.source_uri, m.source_version, m.source_type, m.metadata "
             "  FROM mem.memories m "
             "  JOIN mem.memory_embeddings e ON e.memory_id = m.id "
             f" WHERE {' AND '.join(clauses)} "
             " ORDER BY m.recorded_at ASC LIMIT :limit"),
        params).mappings().all()]


def _neighbours(conn: Connection, memory_id: UUID, ids: list[UUID],
                threshold: float) -> list[tuple[UUID, float]]:
    """Memories among `ids` within cosine distance of `memory_id`.

    Compared against the stored vector of the anchor rather than a re-embedding:
    consolidation must group by what retrieval actually sees, not by what a fresh
    embedding call would produce today with a possibly different model.
    """
    if not ids:
        return []
    rows = conn.execute(
        text("WITH anchor AS ("
             "  SELECT embedding FROM mem.memory_embeddings WHERE memory_id = :i "
             "  ORDER BY created_at DESC LIMIT 1) "
             "SELECT e.memory_id, 1 - (e.embedding <=> (SELECT embedding FROM anchor)) "
             "         AS similarity "
             "  FROM mem.memory_embeddings e "
             " WHERE e.memory_id = ANY(CAST(:ids AS uuid[])) "
             "   AND e.memory_id <> :i "
             "   AND 1 - (e.embedding <=> (SELECT embedding FROM anchor)) >= :threshold "
             " ORDER BY similarity DESC"),
        {"i": str(memory_id), "ids": [str(x) for x in ids], "threshold": threshold},
    ).all()
    return [(UUID(str(r[0])), float(r[1])) for r in rows]


def _survivor(group: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick what the group collapses onto.

    Highest trust first — the blueprint's "preserving the highest trust" — then
    most retrieved, then oldest. Oldest last rather than first because keeping
    the original id would otherwise discard a later, better-attested version of
    the same claim purely for being younger.
    """
    return max(group, key=lambda m: (
        TIER_RANK.get(m["tier"], 0),
        int(m["retrieval_count"] or 0),
        -m["recorded_at"].timestamp(),
    ))


def dedup(conn: Connection, *, tenant_id: UUID, project_id: UUID) -> dict[str, Any]:
    """Collapse near-identical Plane B memories onto one survivor each."""
    cfg = settings()
    threshold = float(cfg.consolidation_dedup_cosine)
    limit = int(cfg.consolidation_batch_size)
    parameters = {"cosine": threshold, "batch_size": limit,
                  "types": list(DEDUP_TYPES)}
    run_id = _open_run(conn, tenant_id=tenant_id, project_id=project_id,
                       kind="dedup", parameters=parameters)

    rows = _candidates(conn, tenant_id=tenant_id, project_id=project_id,
                       types=DEDUP_TYPES, limit=limit)
    by_id = {UUID(str(r["id"])): r for r in rows}
    remaining = list(by_id)
    collapsed: list[dict[str, Any]] = []
    seen: set[UUID] = set()

    for mid in list(remaining):
        if mid in seen:
            continue
        pool = [x for x in remaining if x not in seen and x != mid]
        near = _neighbours(conn, mid, pool, threshold)
        if not near:
            seen.add(mid)
            continue

        group = [by_id[mid]] + [by_id[nid] for nid, _ in near]
        survivor = _survivor(group)
        survivor_id = UUID(str(survivor["id"]))
        losers = [m for m in group if UUID(str(m["id"])) != survivor_id]

        # Provenance is merged onto the survivor, not dropped: the fact that four
        # sessions independently observed the same thing is evidence, and after
        # this pass the survivor is the only row left carrying it.
        merged = (survivor["metadata"] or {}).get("merged_from", [])
        merged.extend({
            "id": str(m["id"]), "source_uri": m["source_uri"],
            "source_version": m["source_version"], "source_type": m["source_type"],
            "recorded_at": m["recorded_at"].isoformat(), "tier": m["tier"],
        } for m in losers)
        conn.execute(
            text("UPDATE mem.memories "
                 "   SET metadata = metadata || CAST(:m AS jsonb) "
                 " WHERE id = :i"),
            {"m": json.dumps({"merged_from": merged,
                              "merged_count": len(merged)}),
             "i": str(survivor_id)},
        )
        for m in losers:
            _archive(conn, UUID(str(m["id"])), survivor_id,
                     f"near-duplicate collapsed by consolidation (cosine >= {threshold})",
                     tenant_id)
            seen.add(UUID(str(m["id"])))
        seen.add(survivor_id)
        collapsed.append({"survivor": str(survivor_id),
                          "archived": [str(m["id"]) for m in losers]})

    affected = sum(len(g["archived"]) for g in collapsed)
    _close_run(conn, run_id, examined=len(rows), affected=affected,
               details={"groups": collapsed[:500]})
    if affected:
        log.info("dedup collapsed %d memory(s) into %d survivor(s)",
                 affected, len(collapsed))
    return {"examined": len(rows), "groups": len(collapsed), "archived": affected,
            "run_id": str(run_id)}


def _summary_text(group: list[dict[str, Any]]) -> tuple[str, str]:
    """Build the summary memory's title and body from the group, extractively.

    Deliberately NOT an LLM call. Consolidation runs unattended over a whole
    project, so a generative summary would introduce a hallucination surface into
    durable memory with no reviewer between it and retrieval, and would make the
    pass non-idempotent — the same episodes would produce different text on every
    run, which is the "context collapse" failure the blueprint names in §2.
    An extractive summary is reproducible and cites its own sources.

    An LLM-written version belongs behind the review inbox as a proposal, not
    here.
    """
    ordered = sorted(group, key=lambda m: m["recorded_at"])
    first, last = ordered[0]["recorded_at"], ordered[-1]["recorded_at"]
    medoid = _survivor(group)
    title = f"Summary of {len(group)} similar episodes: {medoid['title']}"[:200]

    lines = [
        f"Consolidated summary of {len(group)} similar memories recorded between "
        f"{first.date().isoformat()} and {last.date().isoformat()}.",
        "",
        "Originals (archived, still queryable by id and by as_of):",
    ]
    for m in ordered:
        digest = (m["digest"] or m["content"] or "").strip().replace("\n", " ")
        lines.append(f"- [{m['recorded_at'].date().isoformat()}] "
                     f"{m['title']}: {digest[:180]}")
    return title, "\n".join(lines)


def compact_episodes(conn: Connection, *, tenant_id: UUID,
                     project_id: UUID) -> dict[str, Any]:
    """Fold groups of similar, aged episodes into one summary memory each."""
    from . import memories as _memories

    cfg = settings()
    threshold = float(cfg.consolidation_compact_cosine)
    min_group = int(cfg.consolidation_min_episodes)
    age_days = int(cfg.consolidation_age_days)
    limit = int(cfg.consolidation_batch_size)
    parameters = {"cosine": threshold, "min_episodes": min_group,
                  "age_days": age_days, "batch_size": limit}
    run_id = _open_run(conn, tenant_id=tenant_id, project_id=project_id,
                       kind="episode_compaction", parameters=parameters)

    rows = _candidates(conn, tenant_id=tenant_id, project_id=project_id,
                       types=COMPACT_TYPES, older_than_days=age_days, limit=limit)
    by_id = {UUID(str(r["id"])): r for r in rows}
    remaining = list(by_id)
    seen: set[UUID] = set()
    summaries: list[dict[str, Any]] = []

    for mid in list(remaining):
        if mid in seen:
            continue
        pool = [x for x in remaining if x not in seen and x != mid]
        near = _neighbours(conn, mid, pool, threshold)
        group = [by_id[mid]] + [by_id[nid] for nid, _ in near]
        if len(group) < min_group:
            seen.add(mid)
            continue

        title, body = _summary_text(group)
        # The summary can never be more trusted than what it summarises, and a
        # system-written aggregate is not an observation anyone made.
        result = _memories.write_memory(
            conn, tenant_id=tenant_id, project_id=project_id, principal_id=None,
            mtype="session_summary", title=title, content=body,
            # See TIER_BY_SOURCE: `consolidation` maps to `observed`, which is at
            # or below every member (candidates are active, so never quarantined)
            # and, critically, is not itself quarantined — compaction archives the
            # originals, and a quarantined summary would take the whole group out
            # of retrieval and put nothing back.
            source_type="consolidation",
            metadata={"consolidation": {
                "kind": "episode_compaction", "run_id": str(run_id),
                "group_size": len(group),
                "sources": [str(m["id"]) for m in group]}},
        )
        summary_id = UUID(str(result["id"]))
        for m in group:
            _archive(conn, UUID(str(m["id"])), summary_id,
                     f"compacted into summary by consolidation "
                     f"({len(group)} episodes, cosine >= {threshold})",
                     tenant_id)
            seen.add(UUID(str(m["id"])))
        summaries.append({"summary": str(summary_id),
                          "archived": [str(m["id"]) for m in group]})

    affected = sum(len(s["archived"]) for s in summaries)
    _close_run(conn, run_id, examined=len(rows), affected=affected,
               details={"summaries": summaries[:500]})
    if affected:
        log.info("compaction folded %d episode(s) into %d summary memory(s)",
                 affected, len(summaries))
    return {"examined": len(rows), "summaries": len(summaries),
            "archived": affected, "run_id": str(run_id)}
