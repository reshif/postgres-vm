"""Let search_hybrid see quarantined rows, so include_unverified can work.

02-MCP-CONTRACT.md gives both memory.context and memory.search an
`include_unverified` parameter, and 00-MASTER-BLUEPRINT.md §392 is specific about
what it does:

    "Retrieval defaults to tiers >= 2. Tier 1 is surfaced to agents ONLY when the
     caller passes include_unverified=true and it is rendered inside an explicit
     <unverified> block in the context pack."

mem.search_hybrid could not do that. Its `scoped` CTE hard-coded
`m.status = 'active'`, and tier-1 memories are written with status
'quarantined' (memories.QUARANTINE_TIERS), so every arm filtered them out before
p_min_tier was ever consulted. Passing include_unverified=true changed the tier
floor and returned exactly the same rows — a parameter that reads as supported,
silently does nothing, and is impossible to distinguish from "there happened to
be no unverified matches".

This adds p_statuses, defaulting to '{active}' so every existing caller keeps its
current behaviour. Only a caller that explicitly asks gets quarantined rows, and
context.build_pack marks them `unverified` on the way out.

The overload with the old signature is dropped, rather than left in place, so
nothing keeps silently resolving to the version that cannot honour the flag.

Revision ID: 0008
Revises: 0007
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


FUNCTION = """
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
  -- Single definition of "what this principal may see, right now".
  -- RLS also enforces this; belt and braces, and it lets the planner prune early.
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
lex AS (
  SELECT s.id,
         row_number() OVER (
           ORDER BY ts_rank_cd(s.content_tsv, websearch_to_tsquery('english', p_query_text)) DESC
         ) AS rk
    FROM scoped s
   WHERE s.content_tsv @@ websearch_to_tsquery('english', p_query_text)
   LIMIT 60
),
ident AS (
  SELECT s.id,
         row_number() OVER (ORDER BY similarity(s.identifiers, p_query_text) DESC) AS rk
    FROM scoped s
   -- Single %, not %%: this goes through cur.execute() with no parameters, so
   -- psycopg uses the simple query protocol and does no placeholder
   -- substitution. Doubling it produces a literal %% and
   -- `operator does not exist: text %% text`.
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

DROP_OLD = """
DROP FUNCTION IF EXISTS mem.search_hybrid(
  halfvec, text, mem.memory_type[], mem.trust_tier, timestamptz, uuid[], integer, integer);
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(FUNCTION)
        # Drop the 8-arg overload: leaving it would let existing call sites keep
        # resolving to the version that ignores p_statuses, which is exactly the
        # silent behaviour this migration exists to remove.
        cur.execute(DROP_OLD)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(
            "DROP FUNCTION IF EXISTS mem.search_hybrid("
            "halfvec, text, mem.memory_type[], mem.trust_tier, timestamptz, "
            "uuid[], integer, integer, mem.memory_status[]);"
        )
