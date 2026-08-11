"""Revive the identifier retrieval arm: word_similarity, not similarity.

ADR-0008 lists the trigram/identifier arm and calls it "not optional for this
product" — exact codes, file paths, symbols and version strings are what a
developer actually pastes into a query. eval/RESULTS.md recorded it contributing
0% of expected hits across every case, and it stayed that way.

The cause is one operator. The arm matched with `s.identifiers % p_query_text`
and ranked by `similarity(s.identifiers, p_query_text)`. Trigram `similarity` is
symmetric and normalises over the UNION of both strings' trigrams, so a memory
listing many identifiers is compared as a whole against a short query and scores
badly for having been well indexed. The more identifiers a document carries, the
less findable it becomes — exactly inverted.

Measured, on the eval corpus:

    conventions.md identifiers contain `memory_app PgBouncer` VERBATIM
      similarity(identifiers, 'memory_app PgBouncer')       = 0.1927
      word_similarity('memory_app PgBouncer', identifiers)  = 1.0000
      pg_trgm.similarity_threshold                          = 0.3

0.1927 is below the threshold, so `%` was false and the arm returned nothing at
all — for a query whose terms appear in the document character for character.

`word_similarity(query, text)` finds the best matching extent of `query` inside
`text`, which is the question this arm is actually asking: does this memory
mention the identifiers the caller typed. The `<%` operator applies the separate
`pg_trgm.word_similarity_threshold` (default 0.6) and is supported by the
existing `gin_trgm_ops` index on `identifiers`, so no index change is needed.

Argument order matters and is easy to get backwards: `word_similarity(a, b)`
looks for `a` INSIDE `b`, so the query comes first and the document second.

Revision ID: 0043
Revises: 0041
"""
from alembic import op

revision = "0043"
down_revision = "0041"
branch_labels = None
depends_on = None


SQL = """
CREATE OR REPLACE FUNCTION mem.search_hybrid(p_query_embedding halfvec, p_query_text text, p_types mem.memory_type[] DEFAULT NULL::mem.memory_type[], p_min_tier mem.trust_tier DEFAULT 'observed'::mem.trust_tier, p_as_of timestamp with time zone DEFAULT now(), p_entity_ids uuid[] DEFAULT NULL::uuid[], p_k integer DEFAULT 20, p_rrf_k integer DEFAULT 60, p_statuses mem.memory_status[] DEFAULT ARRAY['active'::mem.memory_status])
 RETURNS TABLE(memory_id uuid, rrf_score double precision, r_vec integer, r_lex integer, r_ident integer, r_graph integer, r_time integer)
 LANGUAGE sql
 STABLE
AS $function$
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
  -- word_similarity, NOT similarity. `identifiers % query` compares WHOLE
  -- STRINGS, and trigram similarity normalises over the union of both, so a
  -- document listing many identifiers scores LOWER against a short query: the
  -- more identifiers a memory has, the less findable it becomes, which is
  -- precisely backwards for this arm.
  SELECT s.id,
         row_number() OVER (
           ORDER BY word_similarity(p_query_text, s.identifiers) DESC) AS rk
    FROM scoped s
   WHERE p_query_text <> '' AND p_query_text <% s.identifiers
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
$function$
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    # Deliberately not reverting to `%`. Restoring the old operator would restore
    # an arm that silently returns nothing, which is worse than either working or
    # being absent. Re-run the previous migration's definition if it is genuinely
    # wanted.
    pass
