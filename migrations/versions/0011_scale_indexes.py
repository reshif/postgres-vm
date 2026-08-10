"""Scale-out indexes (Phase 9).

ADR-0001 is specific about what pgvector needs under a scope predicate:

    "Composite B-tree indexes with `tenant_id` as the leading column are
     required so RLS predicates can be satisfied by an index scan... Vector
     queries combined with scope predicates must run with
     hnsw.iterative_scan = 'relaxed_order'."

The GUC is already set inside fn_set_scope. The indexes were not created.

WHY tenant_id MUST LEAD. Every query in this system runs under an RLS predicate
that starts with `tenant_id = mem.current_tenant()`. An index that does not lead
with tenant_id cannot satisfy that predicate, so Postgres filters after the scan
— which on a vector query means the ANN scan returns its top-K globally and then
throws most of it away, returning far fewer rows than asked for. That is the
overfiltering failure ADR-0001 warns about, and it presents as "search quietly
returns 3 results instead of 20" rather than as an error.

The retrieval_events index is separate: that table grows without bound (one row
per pack) and Phase 9 calls for monthly partitioning. Until it is partitioned, a
(tenant_id, created_at) index is what keeps the debugger's "recent packs" query
from degrading into a sequential scan over history.

Not created here: the per-project partial HNSW index. 01-SCHEMA.sql:317 says
"once a project exceeds ~50k memories", and creating dozens of partial indexes
for projects that hold 30 rows costs write throughput and buys nothing. It is a
runbook step, not a migration.

Revision ID: 0011
Revises: 0010
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


SQL = """
-- Scope-leading composite indexes. Every one starts with tenant_id because
-- every RLS policy does.
CREATE INDEX IF NOT EXISTS idx_mem_scope_active
  ON mem.memories (tenant_id, project_id, status, type)
  WHERE upper(valid_at) IS NULL;

CREATE INDEX IF NOT EXISTS idx_mem_scope_recent
  ON mem.memories (tenant_id, project_id, recorded_at DESC);

-- The embeddings join is on (memory_id, model_id) but always filtered by
-- tenant; leading with tenant_id lets the RLS predicate be satisfied by the
-- index instead of by a filter after the ANN scan.
CREATE INDEX IF NOT EXISTS idx_emb_scope
  ON mem.memory_embeddings (tenant_id, model_id, memory_id);

-- entity_mentions is the graph arm's join table and is walked per query.
CREATE INDEX IF NOT EXISTS idx_mentions_entity
  ON mem.entity_mentions (tenant_id, entity_id, weight DESC);

CREATE INDEX IF NOT EXISTS idx_rel_source
  ON mem.relationships (tenant_id, source_id, relation);
CREATE INDEX IF NOT EXISTS idx_rel_target
  ON mem.relationships (tenant_id, target_id, relation);

-- Unbounded-growth table: one row per context pack. Partitioning is Phase 9;
-- this keeps the debugger usable until then.
CREATE INDEX IF NOT EXISTS idx_retrieval_recent
  ON mem.retrieval_events (tenant_id, created_at DESC);

-- Open conflicts are read on every pack build, and the vast majority of rows
-- are resolved. A partial index keeps that lookup proportional to the number of
-- OPEN conflicts rather than to all conflicts ever detected.
CREATE INDEX IF NOT EXISTS idx_conflicts_open
  ON mem.conflicts (tenant_id, project_id)
  WHERE resolution IS NULL;

-- Queue depth is polled by admission control on every write.
CREATE INDEX IF NOT EXISTS idx_ingestion_outcome
  ON mem.ingestion_events (tenant_id, outcome, observed_at DESC);
"""

DOWN = """
DROP INDEX IF EXISTS mem.idx_mem_scope_active;
DROP INDEX IF EXISTS mem.idx_mem_scope_recent;
DROP INDEX IF EXISTS mem.idx_emb_scope;
DROP INDEX IF EXISTS mem.idx_mentions_entity;
DROP INDEX IF EXISTS mem.idx_rel_source;
DROP INDEX IF EXISTS mem.idx_rel_target;
DROP INDEX IF EXISTS mem.idx_retrieval_recent;
DROP INDEX IF EXISTS mem.idx_conflicts_open;
DROP INDEX IF EXISTS mem.idx_ingestion_outcome;
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(DOWN)
