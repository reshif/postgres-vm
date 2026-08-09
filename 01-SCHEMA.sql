-- =====================================================================================
-- UNIVERSAL AGENT MEMORY PLATFORM — CORE SCHEMA
-- Target: PostgreSQL 18+ (temporal constraints), pgvector >= 0.8.2
-- This is the Phase 1–4 schema. Comments mark which phase introduces each object.
--
-- CONVENTIONS
--   * Every tenant-scoped table has tenant_id and FORCE ROW LEVEL SECURITY.
--   * The application role is NOT the table owner (owner bypasses RLS without FORCE,
--     and even with FORCE you want separation).
--   * Scope context is set per TRANSACTION with SET LOCAL / set_config(..., true).
--   * Composite indexes lead with tenant_id so RLS predicates stay index-backed.
-- =====================================================================================

-- ------------------------------------------------------------------ extensions
CREATE EXTENSION IF NOT EXISTS vector;        -- pgvector >= 0.8.2
CREATE EXTENSION IF NOT EXISTS pg_trgm;       -- identifier / fuzzy matching
CREATE EXTENSION IF NOT EXISTS btree_gist;    -- required for WITHOUT OVERLAPS
CREATE EXTENSION IF NOT EXISTS pgcrypto;
-- Optional, only after the eval harness proves the lexical arm is the bottleneck:
-- CREATE EXTENSION IF NOT EXISTS pg_search;  -- ParadeDB BM25 (@@@ operator)

-- ------------------------------------------------------------------ roles
-- Run once, out of band:
--   CREATE ROLE memory_owner LOGIN PASSWORD '...';          -- owns DDL
--   CREATE ROLE memory_app   LOGIN PASSWORD '...' NOBYPASSRLS;
--   CREATE ROLE memory_ro    LOGIN PASSWORD '...' NOBYPASSRLS;  -- console/read replica
--   REVOKE ALL ON SCHEMA public FROM PUBLIC;

CREATE SCHEMA IF NOT EXISTS mem;
SET search_path = mem, public;

-- ------------------------------------------------------------------ enums
CREATE TYPE memory_type AS ENUM (
  'decision','procedure','convention','preference','constraint',
  'episode','failure','success','observation','entity_fact','session_summary'
);

CREATE TYPE memory_status AS ENUM (
  'active','superseded','archived','disputed','quarantined','deleted'
);

CREATE TYPE trust_tier AS ENUM (           -- see blueprint §3.4
  'untrusted',      -- 0
  'inferred',       -- 1
  'observed',       -- 2
  'verified',       -- 3
  'authoritative'   -- 4
);

CREATE TYPE scope_kind AS ENUM ('organization','project','user','session','task');
CREATE TYPE sensitivity AS ENUM ('public','internal','confidential','restricted');
CREATE TYPE actor_type AS ENUM ('human','agent','service','system');

-- =====================================================================================
-- 1. TENANCY, PRINCIPALS, PROJECTS                                        [Phase 1]
-- =====================================================================================

CREATE TABLE organizations (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug          text NOT NULL UNIQUE,
  name          text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- tenant_id == organizations.id throughout. Named tenant_id for RLS clarity.

CREATE TABLE principals (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  actor         actor_type NOT NULL,
  external_id   text NOT NULL,              -- oidc sub, agent id, service name
  display_name  text NOT NULL,
  agent_type    text,                       -- 'claude-code','cursor','codex','hermes',...
  created_at    timestamptz NOT NULL DEFAULT now(),
  disabled_at   timestamptz,
  UNIQUE (tenant_id, actor, external_id)
);

CREATE TABLE projects (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  slug          text NOT NULL,
  name          text NOT NULL,
  repo_url      text,                       -- canonical binding key
  profile       jsonb NOT NULL DEFAULT '{}',-- mirrored .memory/project.yaml
  profile_version text,                     -- git sha of the profile file
  status        text NOT NULL DEFAULT 'active',
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, slug)
);
CREATE INDEX ON projects (tenant_id, repo_url);

-- Explicit, expiring, attributed cross-scope grants. Rows, not code.       [Phase 2]
CREATE TABLE scope_grants (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  from_kind     scope_kind NOT NULL,
  from_id       uuid NOT NULL,
  to_kind       scope_kind NOT NULL,
  to_id         uuid NOT NULL,
  permission    text NOT NULL CHECK (permission IN ('read','write','promote')),
  reason        text NOT NULL,
  granted_by    uuid NOT NULL REFERENCES principals(id),
  granted_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz,                -- NULL requires a second approver (app-enforced)
  revoked_at    timestamptz
);
CREATE INDEX ON scope_grants (tenant_id, to_kind, to_id) WHERE revoked_at IS NULL;

-- =====================================================================================
-- 2. MEMORIES (bi-temporal core)                                          [Phase 1]
--    valid_at   = VALID / application time  (when the statement is true)
--    recorded_at= TRANSACTION time          (when we learned it)
-- =====================================================================================

CREATE TABLE memories (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,

  -- stable logical identity across validity periods and versions
  memory_key        text NOT NULL,

  type              memory_type NOT NULL,
  title             text NOT NULL CHECK (length(title) <= 200),
  content           text NOT NULL CHECK (length(content) <= 8000),
  digest            text NOT NULL CHECK (length(digest) <= 400),

  -- scope --------------------------------------------------------------------
  scope_kind        scope_kind NOT NULL,
  project_id        uuid REFERENCES projects(id) ON DELETE CASCADE,
  owner_principal   uuid REFERENCES principals(id),   -- for scope_kind='user'
  sensitivity       sensitivity NOT NULL DEFAULT 'internal',

  -- trust --------------------------------------------------------------------
  tier              trust_tier NOT NULL,
  confidence        real NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  verification      text,                     -- 'human_confirmed','ci_verified',...

  -- provenance ---------------------------------------------------------------
  source_type       text NOT NULL,            -- git|mcp|session|ci|issue|doc|code|manual
  source_uri        text,
  source_version    text,
  extractor         text,
  asserted_by       uuid REFERENCES principals(id),

  -- temporal -----------------------------------------------------------------
  valid_at          tstzrange NOT NULL DEFAULT tstzrange(now(), NULL, '[)'),
  recorded_at       timestamptz NOT NULL DEFAULT now(),
  superseded_at     timestamptz,
  last_accessed_at  timestamptz,

  -- ranking inputs -----------------------------------------------------------
  status            memory_status NOT NULL DEFAULT 'active',
  importance_prior  real NOT NULL DEFAULT 0.5 CHECK (importance_prior BETWEEN 0 AND 1),
  utility           real NOT NULL DEFAULT 0.0,
  retrieval_count   integer NOT NULL DEFAULT 0,
  pinned            boolean NOT NULL DEFAULT false,

  -- derived ------------------------------------------------------------------
  token_cost        integer NOT NULL DEFAULT 0,
  content_hash      text NOT NULL,
  identifiers       text NOT NULL DEFAULT '',  -- extracted symbols/errors/paths for trigram
  content_tsv       tsvector GENERATED ALWAYS AS (
                       setweight(to_tsvector('english', coalesce(title,'')),   'A') ||
                       setweight(to_tsvector('english', coalesce(digest,'')),  'B') ||
                       setweight(to_tsvector('english', coalesce(content,'')), 'C')
                     ) STORED,
  metadata          jsonb NOT NULL DEFAULT '{}',

  CONSTRAINT project_scope_needs_project
    CHECK (scope_kind <> 'project' OR project_id IS NOT NULL),
  CONSTRAINT quarantine_tier_consistency
    CHECK (status <> 'quarantined' OR tier IN ('untrusted','inferred')),

  -- PG18 temporal uniqueness: one active statement per logical key per instant.
  -- The range column MUST be last, and requires btree_gist.
  -- See the PG<18 emulation branch below (ADR-0006) if this fails to parse.
  CONSTRAINT memories_temporal_uniq
    UNIQUE (tenant_id, memory_key, valid_at WITHOUT OVERLAPS)
);

-- ---------------------------------------------------------------------------
-- ADR-0006 portability branch: PostgreSQL < 18
--
-- WITHOUT OVERLAPS is PG18+. If the target distribution has not shipped it,
-- keep the *model* identical (same column, same semantics, same application
-- code) and enforce non-overlap with an exclusion constraint instead. This is
-- a mechanism swap, not a model change — nothing above or below this block
-- differs, and the migration to the native constraint is a one-line change.
--
--   -- migration 0001b_temporal_emulation.sql  (run INSTEAD OF the constraint above)
--   ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_temporal_uniq;
--   ALTER TABLE memories ADD CONSTRAINT memories_temporal_excl
--     EXCLUDE USING gist (
--       tenant_id  WITH =,
--       memory_key WITH =,
--       valid_at   WITH &&
--     );
--
-- Differences to be aware of while on this branch:
--   * The exclusion constraint is not a UNIQUE constraint, so it cannot be the
--     target of a foreign key. Nothing in this schema needs that — FKs point at
--     the surrogate `id` — but do not add one that points at (memory_key, valid_at).
--   * PERIOD foreign keys (temporal FK) are unavailable. `relationships` uses the
--     same pattern; apply the same swap there.
--   * PG19's UPDATE/DELETE ... FOR PORTION OF is unavailable on either branch
--     below 19. Until then, splitting a validity period is done in application
--     code: close the old row (set valid_at upper bound) and insert the new one
--     inside one transaction. Write it once, in the memory engine, not per caller.
--
-- Migration path to PG18+:
--   BEGIN;
--     ALTER TABLE memories DROP CONSTRAINT memories_temporal_excl;
--     ALTER TABLE memories ADD  CONSTRAINT memories_temporal_uniq
--       UNIQUE (tenant_id, memory_key, valid_at WITHOUT OVERLAPS);
--   COMMIT;
-- Verify first that no overlapping ranges exist (the exclusion constraint
-- guarantees this, so the migration should be mechanical):
--   SELECT a.tenant_id, a.memory_key FROM memories a JOIN memories b
--     ON a.tenant_id=b.tenant_id AND a.memory_key=b.memory_key AND a.id<>b.id
--    AND a.valid_at && b.valid_at;
--
-- CI must run the temporal test suite against BOTH branches for as long as any
-- deployment is below PG18. A model that is only correct on one branch is a
-- model that will break on the migration.
-- ---------------------------------------------------------------------------

-- Supersession is an explicit graph, not an implicit ordering.
CREATE TABLE memory_supersessions (
  tenant_id     uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  new_id        uuid NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  old_id        uuid NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  reason        text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (new_id, old_id)
);

-- Transaction-time history. PG18 gives valid time declaratively but NOT system
-- versioning, so we maintain it ourselves. Append-only; never updated.
CREATE TABLE memory_versions (
  id            bigserial PRIMARY KEY,
  tenant_id     uuid NOT NULL,
  memory_id     uuid NOT NULL,
  version       integer NOT NULL,
  operation     text NOT NULL CHECK (operation IN ('insert','update','status_change','delete')),
  snapshot      jsonb NOT NULL,
  changed_by    uuid,
  changed_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (memory_id, version)
);

CREATE OR REPLACE FUNCTION mem.fn_memory_version() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v integer;
BEGIN
  SELECT coalesce(max(version),0)+1 INTO v FROM mem.memory_versions
   WHERE memory_id = COALESCE(NEW.id, OLD.id);
  INSERT INTO mem.memory_versions (tenant_id, memory_id, version, operation, snapshot, changed_by)
  VALUES (
    COALESCE(NEW.tenant_id, OLD.tenant_id),
    COALESCE(NEW.id, OLD.id),
    v,
    lower(TG_OP),
    to_jsonb(COALESCE(NEW, OLD)),
    nullif(current_setting('app.principal_id', true),'')::uuid
  );
  RETURN COALESCE(NEW, OLD);
END $$;

CREATE TRIGGER trg_memory_version
AFTER INSERT OR UPDATE OR DELETE ON memories
FOR EACH ROW EXECUTE FUNCTION mem.fn_memory_version();

-- ------------------------------------------------------------------ indexes
-- tenant_id leads every hot index so RLS predicates stay index-backed.
CREATE INDEX idx_mem_scope        ON memories (tenant_id, project_id, status, type)
  WHERE status = 'active';
CREATE INDEX idx_mem_key          ON memories (tenant_id, memory_key);
CREATE INDEX idx_mem_recorded     ON memories (tenant_id, project_id, recorded_at DESC);
CREATE INDEX idx_mem_valid        ON memories USING gist (tenant_id, valid_at);
CREATE INDEX idx_mem_tsv          ON memories USING gin (content_tsv);
CREATE INDEX idx_mem_ident_trgm   ON memories USING gin (identifiers gin_trgm_ops);
CREATE INDEX idx_mem_hash         ON memories (tenant_id, content_hash);
CREATE INDEX idx_mem_quarantine   ON memories (tenant_id, project_id, recorded_at)
  WHERE status = 'quarantined';

-- =====================================================================================
-- 3. EMBEDDINGS — separate table so models are swappable and multi-active   [Phase 1]
-- =====================================================================================

CREATE TABLE embedding_models (
  id            text PRIMARY KEY,           -- 'bge-m3@1', 'nomic-embed-text@1.5'
  provider      text NOT NULL,
  dimensions    integer NOT NULL,
  normalized    boolean NOT NULL DEFAULT true,
  is_active     boolean NOT NULL DEFAULT false,
  is_primary    boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- halfvec halves storage for >=1024 dims with negligible recall loss at these sizes.
CREATE TABLE memory_embeddings (
  memory_id     uuid NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  model_id      text NOT NULL REFERENCES embedding_models(id),
  tenant_id     uuid NOT NULL,
  embedding     halfvec(1024) NOT NULL,
  digest_embedding halfvec(1024),           -- used for MMR dedup (cheaper, stabler)
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (memory_id, model_id)
);

CREATE INDEX idx_emb_hnsw ON memory_embeddings
  USING hnsw (embedding halfvec_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- Per-hot-project partial index once a project exceeds ~50k memories:
--   CREATE INDEX CONCURRENTLY idx_emb_hnsw_p_<slug> ON memory_embeddings
--     USING hnsw (embedding halfvec_cosine_ops) WHERE tenant_id = '...';

-- Session GUCs the application MUST set (see fn_set_scope below):
--   hnsw.ef_search = 100
--   hnsw.iterative_scan = 'relaxed_order'   -- pgvector >= 0.8.0; prevents overfiltering
--   hnsw.max_scan_tuples = 20000

-- =====================================================================================
-- 4. ENTITIES AND RELATIONSHIPS (the graph, in Postgres)                   [Phase 4]
-- =====================================================================================

CREATE TABLE entities (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id    uuid REFERENCES projects(id) ON DELETE CASCADE,  -- NULL = org-wide
  kind          text NOT NULL,              -- technology|service|module|person|system|env
  canonical_name text NOT NULL,
  attributes    jsonb NOT NULL DEFAULT '{}',
  tier          trust_tier NOT NULL DEFAULT 'observed',
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, project_id, kind, canonical_name)
);

CREATE TABLE entity_aliases (
  tenant_id     uuid NOT NULL,
  entity_id     uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  alias         text NOT NULL,
  PRIMARY KEY (entity_id, alias)
);
CREATE INDEX idx_alias_trgm ON entity_aliases USING gin (alias gin_trgm_ops);

-- Closed ontology. Adding a predicate is a code change + migration, deliberately.
CREATE TYPE relation_type AS ENUM (
  'uses','depends_on','part_of','implements','supersedes','caused_by','solved_by',
  'contradicts','supports','mitigates','owns','deployed_to','produces','documented_in'
);

CREATE TABLE relationships (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id    uuid REFERENCES projects(id) ON DELETE CASCADE,
  source_id     uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  target_id     uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  relation      relation_type NOT NULL,
  tier          trust_tier NOT NULL,
  confidence    real NOT NULL DEFAULT 0.7,
  valid_at      tstzrange NOT NULL DEFAULT tstzrange(now(), NULL, '[)'),
  evidence_memory_id uuid REFERENCES memories(id) ON DELETE SET NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT rel_temporal_uniq
    UNIQUE (tenant_id, source_id, target_id, relation, valid_at WITHOUT OVERLAPS)
    -- PG<18: swap for an EXCLUDE USING gist constraint on
    -- (tenant_id =, source_id =, target_id =, relation =, valid_at &&).
    -- See the ADR-0006 portability branch above.
);
CREATE INDEX ON relationships (tenant_id, source_id, relation);
CREATE INDEX ON relationships (tenant_id, target_id, relation);

-- Inferred edges land here, NOT in relationships, until reviewed.
CREATE TABLE proposed_relationships (LIKE relationships INCLUDING ALL);
ALTER TABLE proposed_relationships ADD COLUMN proposed_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE proposed_relationships ADD COLUMN reviewed_by uuid;

CREATE TABLE entity_mentions (
  tenant_id     uuid NOT NULL,
  memory_id     uuid NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  entity_id     uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  weight        real NOT NULL DEFAULT 1.0,
  PRIMARY KEY (memory_id, entity_id)
);
CREATE INDEX ON entity_mentions (tenant_id, entity_id);

-- =====================================================================================
-- 5. CONFLICTS, FEEDBACK, TELEMETRY                                     [Phase 2–3]
-- =====================================================================================

CREATE TABLE conflicts (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  project_id    uuid REFERENCES projects(id) ON DELETE CASCADE,
  memory_a      uuid NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  memory_b      uuid NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  kind          text NOT NULL,   -- contradiction|stale_supersession|duplicate|scope_mismatch
  detected_by   text NOT NULL,   -- rule id or model id
  detected_at   timestamptz NOT NULL DEFAULT now(),
  resolution    text,            -- a_wins|b_wins|both_valid|merged|escalated
  resolved_by   uuid REFERENCES principals(id),
  resolved_at   timestamptz,
  UNIQUE (memory_a, memory_b, kind)
);
CREATE INDEX ON conflicts (tenant_id, project_id) WHERE resolved_at IS NULL;

CREATE TABLE retrieval_events (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL,
  project_id    uuid,
  principal_id  uuid,
  pack_id       text NOT NULL,
  tool          text NOT NULL,             -- context|search|explain
  query_text    text,
  plan          jsonb NOT NULL,            -- intent, entities, types, window
  arm_results   jsonb NOT NULL,            -- per-arm ids + ranks
  fused         jsonb NOT NULL,            -- id -> {rrf, features, final}
  dropped       jsonb NOT NULL DEFAULT '[]',-- id -> reason (budget|dupe|scope|tier)
  returned_ids  uuid[] NOT NULL,
  token_count   integer NOT NULL,
  ranking_profile text NOT NULL,
  latency_ms    jsonb NOT NULL,            -- per stage
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON retrieval_events (tenant_id, project_id, created_at DESC);
-- High-churn table: partition by month from day one if you expect >10M rows/yr.

CREATE TABLE feedback (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL,
  memory_id     uuid REFERENCES memories(id) ON DELETE CASCADE,
  pack_id       text,
  principal_id  uuid,
  signal        text NOT NULL CHECK (signal IN ('useful','irrelevant','wrong','missing','pin','unpin')),
  weight        real NOT NULL DEFAULT 1.0, -- agent-sourced feedback is advisory: weight < 1
  note          text,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON feedback (tenant_id, memory_id);

CREATE TABLE audit_log (
  id            bigserial PRIMARY KEY,
  tenant_id     uuid NOT NULL,
  principal_id  uuid,
  action        text NOT NULL,   -- memory.created|promoted|deleted|grant.created|scope.denied|...
  object_type   text NOT NULL,
  object_id     text,
  scope_context jsonb NOT NULL,
  outcome       text NOT NULL,   -- allow|deny|error
  detail        jsonb NOT NULL DEFAULT '{}',
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON audit_log (tenant_id, created_at DESC);
CREATE INDEX ON audit_log (tenant_id, action, created_at DESC);

CREATE TABLE ingestion_events (      -- append-only; the rebuild source of truth
  id            bigserial PRIMARY KEY,
  tenant_id     uuid NOT NULL,
  project_id    uuid,
  source_type   text NOT NULL,
  source_uri    text NOT NULL,
  source_version text,
  content_hash  text NOT NULL,
  payload       jsonb NOT NULL,
  occurred_at   timestamptz NOT NULL,
  observed_at   timestamptz NOT NULL DEFAULT now(),
  processed_at  timestamptz,
  outcome       text,
  UNIQUE (tenant_id, source_uri, content_hash)
);

CREATE TABLE ranking_profiles (
  id            text PRIMARY KEY,          -- 'default@7'
  weights       jsonb NOT NULL,
  active        boolean NOT NULL DEFAULT false,
  eval_score    jsonb,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- =====================================================================================
-- 6. ROW LEVEL SECURITY                                                    [Phase 1]
-- =====================================================================================

-- Transaction-scoped scope context. `true` = local to transaction: MANDATORY under
-- transaction-mode pooling, otherwise context leaks to the next client on that conn.
CREATE OR REPLACE FUNCTION mem.fn_set_scope(
  p_tenant uuid, p_principal uuid, p_project uuid, p_projects uuid[]
) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  PERFORM set_config('app.tenant_id',     p_tenant::text,    true);
  PERFORM set_config('app.principal_id',  p_principal::text, true);
  PERFORM set_config('app.project_id',    coalesce(p_project::text,''), true);
  PERFORM set_config('app.project_ids',   array_to_string(p_projects, ','), true);
  PERFORM set_config('hnsw.ef_search',        '100',            true);
  PERFORM set_config('hnsw.iterative_scan',   'relaxed_order',  true);
  PERFORM set_config('hnsw.max_scan_tuples',  '20000',          true);
END $$;
REVOKE EXECUTE ON FUNCTION mem.fn_set_scope FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION mem.fn_set_scope TO memory_app;

CREATE OR REPLACE FUNCTION mem.current_tenant() RETURNS uuid
LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('app.tenant_id', true),'')::uuid $$;

CREATE OR REPLACE FUNCTION mem.allowed_projects() RETURNS uuid[]
LANGUAGE sql STABLE AS $$
  SELECT coalesce(
    (SELECT array_agg(x::uuid)
       FROM unnest(string_to_array(nullif(current_setting('app.project_ids', true),''), ',')) x),
    ARRAY[]::uuid[])
$$;

ALTER TABLE memories            ENABLE ROW LEVEL SECURITY;
ALTER TABLE memories            FORCE  ROW LEVEL SECURITY;  -- without FORCE the owner bypasses
ALTER TABLE memory_embeddings   ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_embeddings   FORCE  ROW LEVEL SECURITY;
ALTER TABLE entities            ENABLE ROW LEVEL SECURITY;
ALTER TABLE entities            FORCE  ROW LEVEL SECURITY;
ALTER TABLE relationships       ENABLE ROW LEVEL SECURITY;
ALTER TABLE relationships       FORCE  ROW LEVEL SECURITY;
ALTER TABLE entity_mentions     ENABLE ROW LEVEL SECURITY;
ALTER TABLE entity_mentions     FORCE  ROW LEVEL SECURITY;
ALTER TABLE conflicts           ENABLE ROW LEVEL SECURITY;
ALTER TABLE conflicts           FORCE  ROW LEVEL SECURITY;
ALTER TABLE retrieval_events    ENABLE ROW LEVEL SECURITY;
ALTER TABLE retrieval_events    FORCE  ROW LEVEL SECURITY;
ALTER TABLE feedback            ENABLE ROW LEVEL SECURITY;
ALTER TABLE feedback            FORCE  ROW LEVEL SECURITY;
-- ... apply to every tenant-scoped table. A CI test asserts none are missed.

-- READ: tenant match AND (org-wide OR allowed project OR own user-scope).
CREATE POLICY memories_read ON memories FOR SELECT
USING (
  tenant_id = mem.current_tenant()
  AND (
        scope_kind = 'organization'
    OR (scope_kind = 'project' AND project_id = ANY (mem.allowed_projects()))
    OR (scope_kind = 'user'
        AND owner_principal = nullif(current_setting('app.principal_id', true),'')::uuid)
  )
);

-- WRITE: only into the *current* project or own user scope. Never org-wide from an app
-- path; org-scope writes go through a separate privileged role used by the promotion flow.
CREATE POLICY memories_write ON memories FOR INSERT
WITH CHECK (
  tenant_id = mem.current_tenant()
  AND (
        (scope_kind = 'project'
         AND project_id = nullif(current_setting('app.project_id', true),'')::uuid)
    OR  (scope_kind = 'user'
         AND owner_principal = nullif(current_setting('app.principal_id', true),'')::uuid)
  )
);

CREATE POLICY memories_update ON memories FOR UPDATE
USING (tenant_id = mem.current_tenant()
       AND (scope_kind <> 'project' OR project_id = ANY (mem.allowed_projects())))
WITH CHECK (tenant_id = mem.current_tenant());

CREATE POLICY emb_read ON memory_embeddings FOR SELECT
USING (tenant_id = mem.current_tenant()
       AND EXISTS (SELECT 1 FROM memories m WHERE m.id = memory_id));  -- inherits memories RLS

-- Repeat the pattern for entities/relationships/mentions/conflicts/feedback.

GRANT USAGE ON SCHEMA mem TO memory_app, memory_ro;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA mem TO memory_app;
GRANT SELECT ON ALL TABLES IN SCHEMA mem TO memory_ro;
-- No DELETE for the app role. Deletion is an explicit, audited admin operation.

-- =====================================================================================
-- 7. HYBRID RETRIEVAL                                                      [Phase 3]
--    Scope predicates live INSIDE each arm's CTE — never applied post-hoc.
--    Filtering after an ANN scan silently returns too few rows (pgvector overfiltering).
-- =====================================================================================

CREATE OR REPLACE FUNCTION mem.search_hybrid(
  p_query_embedding halfvec(1024),
  p_query_text      text,
  p_types           memory_type[]  DEFAULT NULL,
  p_min_tier        trust_tier     DEFAULT 'observed',
  p_as_of           timestamptz    DEFAULT now(),
  p_entity_ids      uuid[]         DEFAULT NULL,
  p_k               integer        DEFAULT 20,
  p_rrf_k           integer        DEFAULT 60
)
RETURNS TABLE (
  memory_id uuid, rrf_score double precision,
  r_vec int, r_lex int, r_ident int, r_graph int, r_time int
)
LANGUAGE sql STABLE AS $$
WITH scoped AS (
  -- Single definition of "what this principal may see, right now".
  -- RLS also enforces this; belt and braces, and it lets the planner prune early.
  SELECT m.id, m.type, m.content_tsv, m.identifiers, m.recorded_at
    FROM mem.memories m
   WHERE m.status = 'active'
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
   WHERE p_query_text <> '' AND s.identifiers % p_query_text
   LIMIT 30
),
graph AS (
  -- 2-hop neighbourhood of the resolved entities, scope-filtered at every hop.
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
$$;

-- Feature reranking (trust, importance, utility, recency, MMR) happens in the
-- application layer, where weights are versioned and A/B-testable. Keep it out of SQL.

-- ------------------------------------------------------------------ time travel
CREATE OR REPLACE FUNCTION mem.as_of(p_project uuid, p_at timestamptz)
RETURNS SETOF mem.memories
LANGUAGE sql STABLE AS $$
  SELECT * FROM mem.memories
   WHERE project_id = p_project
     AND valid_at @> p_at
     AND recorded_at <= p_at          -- bi-temporal: what we believed AT that time
     AND status <> 'deleted'
$$;

-- =====================================================================================
-- 8. ISOLATION REGRESSION TESTS (pgTAP) — these are a MERGE GATE, not a nice-to-have
-- =====================================================================================
-- Policy regressions are silent: no error, just wrong rows. Run in CI on every PR.
--
--   BEGIN;
--   SELECT plan(6);
--   -- seed: org O, projects A and B, one memory in each
--   SELECT mem.fn_set_scope(:'org', :'principal', :'projA', ARRAY[:'projA']::uuid[]);
--   SELECT is((SELECT count(*) FROM mem.memories WHERE project_id = :'projB'), 0::bigint,
--             'project A context cannot see project B memories');
--   SELECT is((SELECT count(*) FROM mem.memory_embeddings e
--                JOIN mem.memories m ON m.id = e.memory_id
--               WHERE m.project_id = :'projB'), 0::bigint,
--             'embeddings inherit isolation');
--   SELECT throws_ok($$INSERT INTO mem.memories(...project_id := :'projB'...)$$,
--             '42501', NULL, 'cannot write into a project outside current scope');
--   SELECT is((SELECT count(*) FROM mem.memories WHERE status = 'quarantined'), 0::bigint,
--             'quarantined memories are excluded from the default retrieval path');
--   -- assert every tenant-scoped table has FORCE RLS:
--   SELECT is((SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
--               WHERE n.nspname='mem' AND c.relkind='r'
--                 AND c.relrowsecurity = false), 0::bigint,
--             'every table in mem has RLS enabled');
--   SELECT is((SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
--               WHERE n.nspname='mem' AND c.relkind='r'
--                 AND c.relrowsecurity AND NOT c.relforcerowsecurity), 0::bigint,
--             'every RLS table also has FORCE');
--   SELECT * FROM finish();
--   ROLLBACK;
