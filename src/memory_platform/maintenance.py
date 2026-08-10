"""Scheduler maintenance jobs — decay, utility, conflict sweep.

05-BUILD-PLAN Phase 7 and ADR-0009. These run on the scheduler because they are
whole-project passes, not per-request work.

DECAY IS ARCHIVAL, NOT DELETION. 05-BUILD-PLAN Phase 2 caps deterministic
capture with "30-day decay". What decays is an episode's claim on the context
budget, not the record: an episode nobody has retrieved in 30 days is archived,
which removes it from retrieval while leaving it answerable by an `as_of` query.
Deleting it would make "what did we believe in June" unanswerable, which is the
one thing the bi-temporal model exists for.

UTILITY IS LEARNED, IMPORTANCE IS COMPUTED (ADR-0009). importance_prior is a
deterministic function of stable properties and never moves. utility is evidence
of usefulness and only counts INDEPENDENT sessions — an agent that retrieves the
same memory forty times in one session has demonstrated one thing once.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .config import settings

log = logging.getLogger("memory.maintenance")

# Types eligible for decay. Decisions, constraints and conventions never decay:
# a decision nobody asked about this month is not less true.
DECAYABLE = ("episode", "observation", "session_summary")
DECAY_DAYS = 30
UTILITY_MIN_SESSIONS = 5     # ADR-0009 cold-start guard


def decay(conn: Connection, *, tenant_id: UUID, project_id: UUID) -> dict[str, Any]:
    """Archive stale, unused episodes. Closes valid_at so the row stops
    competing for retrieval while remaining reachable through as_of()."""
    rows = conn.execute(
        text("UPDATE mem.memories "
             "   SET status = 'archived', superseded_at = now(), "
             "       valid_at = tstzrange(lower(valid_at), now(), '[)') "
             " WHERE tenant_id = :t AND project_id = :p "
             "   AND status = 'active' AND upper(valid_at) IS NULL "
             "   AND type::text = ANY(:types) "
             "   AND recorded_at < now() - make_interval(days => :days) "
             "   AND retrieval_count = 0 "
             "   AND NOT pinned "
             "RETURNING id"),
        {"t": str(tenant_id), "p": str(project_id),
         "types": list(DECAYABLE), "days": DECAY_DAYS},
    ).scalars().all()
    if rows:
        log.info("decay archived %d stale episode(s)", len(rows))
    return {"archived": len(rows)}


def recompute_utility(conn: Connection, *, tenant_id: UUID,
                      project_id: UUID) -> dict[str, Any]:
    """Recompute learned utility from retrieval and feedback evidence.

    utility = 0.6 * usage + 0.4 * feedback, both in 0..1, and only for memories
    with enough INDEPENDENT sessions to mean anything. Counting distinct packs
    rather than raw retrievals is what stops one enthusiastic session — or one
    agent looping — from manufacturing a highly useful memory (Suite 5's last
    row: "an agent self-reporting its own writes as highly useful, repeatedly").
    """
    rows = conn.execute(
        text("""
        WITH usage AS (
          SELECT m.id,
                 count(DISTINCT re.pack_id) AS sessions
            FROM mem.memories m
            LEFT JOIN mem.retrieval_events re
                   ON m.id = ANY (re.returned_ids)
                  AND re.tenant_id = m.tenant_id
           WHERE m.tenant_id = :t AND m.project_id = :p
           GROUP BY m.id
        ),
        fb AS (
          SELECT memory_id,
                 -- The real signal vocabulary (mem.feedback CHECK):
                 -- useful | irrelevant | wrong | missing | pin | unpin.
                 -- `wrong` is weighted double: a memory that is WRONG is worse
                 -- than one that was merely irrelevant to this query, and
                 -- treating them alike lets bad knowledge survive on volume.
                 sum(CASE WHEN signal = 'useful'     THEN weight
                          WHEN signal = 'pin'        THEN weight
                          WHEN signal = 'irrelevant' THEN -weight
                          WHEN signal = 'wrong'      THEN -2 * weight
                          ELSE 0 END)                       AS score,
                 count(DISTINCT principal_id)               AS voters
            FROM mem.feedback
           WHERE tenant_id = :t AND memory_id IS NOT NULL
           GROUP BY memory_id
        )
        UPDATE mem.memories m
           SET retrieval_count = u.sessions,
               utility = CASE
                 WHEN u.sessions < :min_sessions THEN 0.0
                 ELSE least(1.0,
                        0.6 * least(1.0, u.sessions::float / 20.0)
                      + 0.4 * greatest(0.0, least(1.0,
                              (coalesce(f.score, 0)::float + 5) / 10.0)))
               END
          FROM usage u
          LEFT JOIN fb f ON f.memory_id = u.id
         WHERE m.id = u.id
           AND (m.retrieval_count IS DISTINCT FROM u.sessions
                OR m.utility IS DISTINCT FROM CASE
                     WHEN u.sessions < :min_sessions THEN 0.0
                     ELSE least(1.0,
                            0.6 * least(1.0, u.sessions::float / 20.0)
                          + 0.4 * greatest(0.0, least(1.0,
                                  (coalesce(f.score, 0)::float + 5) / 10.0)))
                   END)
        RETURNING m.id
        """),
        {"t": str(tenant_id), "p": str(project_id),
         "min_sessions": UTILITY_MIN_SESSIONS},
    ).scalars().all()
    return {"updated": len(rows)}


def backfill_embeddings(conn: Connection, *, tenant_id: UUID, project_id: UUID,
                        limit: int = 200) -> dict[str, Any]:
    """Embed memories that have no vector.

    ADR-0008 lets a write succeed when the embedder is unreachable: the memory is
    stored, retrievable through the lexical arm, and left for backfill. Nothing
    was doing the backfill, so an embedder outage left those memories permanently
    invisible to the vector arm — a permanent degradation caused by a transient
    failure. This is not a scale problem; it fires on the next outage.

    Batched, because a backlog of ten thousand is not something to attempt in one
    transaction, and a failure part-way should keep the work already done.
    """
    from . import embeddings as _emb

    rows = conn.execute(
        text("SELECT m.id, m.title, m.digest, m.content "
             "  FROM mem.memories m "
             " WHERE m.tenant_id = :t AND m.project_id = :p "
             "   AND m.status <> 'deleted' AND upper(m.valid_at) IS NULL "
             "   AND NOT EXISTS (SELECT 1 FROM mem.memory_embeddings e "
             "                    WHERE e.memory_id = m.id) "
             " ORDER BY m.recorded_at LIMIT :k"),
        {"t": str(tenant_id), "p": str(project_id), "k": limit},
    ).mappings().all()
    if not rows:
        return {"pending": 0, "embedded": 0}

    model_id = _emb.ensure_registered(conn)
    done = 0
    for r in rows:
        body = (r["title"] or "") + "\n\n" + (r["content"] or "")
        try:
            vecs = _emb.provider().embed([body, r["digest"] or r["title"] or body])
        except _emb.EmbeddingUnavailable as exc:
            # Still down. Stop rather than retrying once per row.
            log.warning("embedding backfill stopped after %d: %s", done, exc)
            break
        conn.execute(
            text("INSERT INTO mem.memory_embeddings "
                 "  (memory_id, model_id, tenant_id, embedding, digest_embedding) "
                 "VALUES (:m, :mo, :t, CAST(:v AS halfvec(1024)), "
                 "        CAST(:d AS halfvec(1024))) "
                 "ON CONFLICT DO NOTHING"),
            {"m": str(r["id"]), "mo": model_id, "t": str(tenant_id),
             "v": _emb.to_pgvector(vecs[0]), "d": _emb.to_pgvector(vecs[1])},
        )
        done += 1
    if done:
        log.info("embedding backfill: %d of %d pending", done, len(rows))
    return {"pending": len(rows), "embedded": done}


def index_advice(conn: Connection, *, tenant_id: UUID, tenant_slug: str = "") -> dict:
    """Report whether this tenant now warrants a partial HNSW index.

    IT ADVISES; IT DOES NOT BUILD. `memory_app` has no DDL rights, and that is
    the point: the application role must not be able to alter the schema that
    isolates it. Granting it CREATE to automate this would trade a real security
    boundary for a convenience. (Attempting it fails with
    `must be owner of table memory_embeddings`, which is the privilege
    separation working.)

    What was actually wrong with the old arrangement was not that a human runs
    the DDL — it is that nothing ever TOLD the human. 01-SCHEMA.sql:317 said
    "once a project exceeds ~50k memories" in a comment, so the index appeared
    only if someone happened to remember. Now the scheduler checks every cycle,
    the threshold is configuration rather than a number in a comment, and the
    advice carries the exact command to run.
    """
    s_ = settings()
    threshold = s_.partial_index_threshold
    if threshold <= 0:
        return {"advised": False, "reason": "disabled"}

    safe = re.sub(r"[^a-z0-9_]", "_", (tenant_slug or str(tenant_id)).lower())[:40]
    name = "idx_emb_hnsw_t_" + safe

    n = conn.execute(
        text("SELECT count(*) FROM mem.memory_embeddings WHERE tenant_id = :t"),
        {"t": str(tenant_id)}).scalar_one()
    exists = conn.execute(
        text("SELECT 1 FROM pg_indexes WHERE schemaname = 'mem' AND indexname = :n"),
        {"n": name}).scalar_one_or_none()

    if exists or n < threshold:
        return {"advised": False, "rows": n, "threshold": threshold,
                "index": name, "exists": bool(exists)}

    command = (
        "CREATE INDEX CONCURRENTLY " + name +
        " ON mem.memory_embeddings USING hnsw (embedding halfvec_cosine_ops)"
        " WHERE tenant_id = '" + str(tenant_id) + "';")
    # WARNING, not INFO: this is a standing performance problem that needs an
    # operator, and it will repeat every cycle until someone acts on it.
    log.warning("partial index advised for tenant %s (%d rows >= %d). Run as "
                "memory_owner: %s", tenant_slug or tenant_id, n, threshold, command)
    return {"advised": True, "rows": n, "threshold": threshold,
            "index": name, "command": command,
            "note": "run as memory_owner; CONCURRENTLY avoids blocking writes"}


def run_all(conn: Connection, *, tenant_id: UUID, project_id: UUID) -> dict[str, Any]:
    """One maintenance pass. Each step is independent: a failure in one must not
    stop the others, because the sweep runs unattended and a decay bug should not
    silently disable conflict detection for a month."""
    from . import conflicts as _conflicts

    out: dict[str, Any] = {}
    for name, fn in (
        ("conflicts", lambda: _conflicts.detect(conn, tenant_id=tenant_id,
                                                project_id=project_id)),
        ("utility", lambda: recompute_utility(conn, tenant_id=tenant_id,
                                              project_id=project_id)),
        ("decay", lambda: decay(conn, tenant_id=tenant_id, project_id=project_id)),
        ("embeddings", lambda: backfill_embeddings(conn, tenant_id=tenant_id,
                                                   project_id=project_id)),
        ("index_advice", lambda: index_advice(conn, tenant_id=tenant_id)),
    ):
        try:
            out[name] = fn()
        except Exception as exc:  # noqa: BLE001
            log.exception("maintenance step %s failed: %s", name, exc)
            out[name] = {"error": str(exc)[:200]}
    return out
