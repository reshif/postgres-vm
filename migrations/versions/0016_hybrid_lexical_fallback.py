"""Add a bounded relaxed lexical arm to hybrid search.

The lexical arm originally used an all-terms web search query only. That is a
good precision-first primary pass, but it disappears entirely when one query
word is absent from a document, leaving vector search as the sole relevance
signal. The degraded path already widens in that situation; healthy retrieval
must do the same without losing the strict result when it exists.

Revision ID: 0016
Revises: 0015
"""
from __future__ import annotations

from alembic import op


revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


FUNCTION = r"""
CREATE OR REPLACE FUNCTION mem.search_hybrid(
  p_query_embedding halfvec(1024),
  p_query_text      text,
  p_types           mem.memory_type[]   DEFAULT NULL,
  p_min_tier        mem.trust_tier      DEFAULT 'observed',
  p_as_of           timestamptz         DEFAULT now(),
  p_entity_ids      uuid[]              DEFAULT NULL,
  p_k               integer             DEFAULT 20,
  p_rrf_k           integer             DEFAULT 60,
  p_statuses        mem.memory_status[] DEFAULT ARRAY['active']::mem.memory_status[]
)
RETURNS TABLE (
  memory_id uuid, rrf_score double precision,
  r_vec int, r_lex int, r_ident int, r_graph int, r_time int
)
LANGUAGE sql STABLE AS $fn$
WITH scoped AS (
  SELECT m.id, m.type, m.content_tsv, m.identifiers, m.recorded_at
    FROM mem.memories m
   WHERE m.status = ANY (p_statuses)
     AND m.tier >= p_min_tier
     AND m.valid_at @> p_as_of
     AND (p_types IS NULL OR m.type = ANY (p_types))
),
vec AS (
  SELECT e.memory_id AS id,
         row_number() OVER (ORDER BY e.embedding <=> p_query_embedding) AS rk
    FROM mem.memory_embeddings e
    JOIN scoped s ON s.id = e.memory_id
   WHERE e.model_id = (SELECT id FROM mem.embedding_models WHERE is_primary LIMIT 1)
   ORDER BY e.embedding <=> p_query_embedding
   LIMIT 60
),
lex_strict AS (
  SELECT s.id, ts_rank_cd(s.content_tsv,
                          websearch_to_tsquery('english', p_query_text)) AS score
    FROM scoped s
   WHERE p_query_text <> ''
     AND s.content_tsv @@ websearch_to_tsquery('english', p_query_text)
   ORDER BY score DESC
   LIMIT 60
),
lex_matches AS (
  SELECT id, score FROM lex_strict
  UNION ALL
  SELECT id, score
    FROM (
      SELECT s.id, ts_rank_cd(
               s.content_tsv,
               to_tsquery('english',
                 btrim(regexp_replace(lower(p_query_text), '[^[:alnum:]_]+', ' | ', 'g'), ' |')
               )
             ) AS score
        FROM scoped s
       WHERE NOT EXISTS (SELECT 1 FROM lex_strict)
         AND p_query_text <> ''
         AND s.content_tsv @@ to_tsquery(
               'english',
               btrim(regexp_replace(lower(p_query_text), '[^[:alnum:]_]+', ' | ', 'g'), ' |')
             )
       ORDER BY score DESC
       LIMIT 60
    ) AS relaxed
),
lex AS (
  SELECT id, row_number() OVER (ORDER BY score DESC) AS rk
    FROM lex_matches
),
ident AS (
  SELECT s.id,
         row_number() OVER (ORDER BY similarity(s.identifiers, p_query_text) DESC) AS rk
    FROM scoped s
   WHERE p_query_text <> '' AND s.identifiers % p_query_text
   LIMIT 30
),
graph AS (
  WITH RECURSIVE nb(entity_id, depth) AS (
      SELECT unnest(coalesce(p_entity_ids, ARRAY[]::uuid[])), 0
    UNION
      SELECT CASE WHEN r.source_id = nb.entity_id THEN r.target_id ELSE r.source_id END,
             nb.depth + 1
        FROM mem.relationships r
        JOIN nb ON nb.entity_id IN (r.source_id, r.target_id)
       WHERE nb.depth < 2
         AND r.tier >= 'observed'
         AND r.valid_at @> p_as_of
  )
  SELECT em.memory_id AS id,
         row_number() OVER (ORDER BY min(nb.depth), sum(em.weight) DESC) AS rk
    FROM mem.entity_mentions em
    JOIN nb ON nb.entity_id = em.entity_id
    JOIN scoped s ON s.id = em.memory_id
   GROUP BY em.memory_id
   LIMIT 40
),
recent AS (
  SELECT s.id, row_number() OVER (ORDER BY s.recorded_at DESC) AS rk
    FROM scoped s
   WHERE s.type IN ('episode','failure','success','decision')
   LIMIT 20
),
fused AS (
  SELECT id,
         sum(w / (p_rrf_k + rk)) AS rrf,
         max(CASE WHEN arm='vec'   THEN rk END) AS r_vec,
         max(CASE WHEN arm='lex'   THEN rk END) AS r_lex,
         max(CASE WHEN arm='ident' THEN rk END) AS r_ident,
         max(CASE WHEN arm='graph' THEN rk END) AS r_graph,
         max(CASE WHEN arm='time'  THEN rk END) AS r_time
    FROM (
      SELECT id, rk, 1.0::float AS w, 'vec'   AS arm FROM vec
      UNION ALL SELECT id, rk, 1.0, 'lex'   FROM lex
      UNION ALL SELECT id, rk, 0.7, 'ident' FROM ident
      UNION ALL SELECT id, rk, 0.6, 'graph' FROM graph
      UNION ALL SELECT id, rk, 0.5, 'time'  FROM recent
    ) arms
   GROUP BY id
)
SELECT id, rrf, r_vec, r_lex, r_ident, r_graph, r_time
  FROM fused
 ORDER BY rrf DESC
 LIMIT p_k;
$fn$;
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cursor:
        cursor.execute(FUNCTION)


def downgrade() -> None:
    raise RuntimeError("Downgrade requires restoring the 0008 function body")
