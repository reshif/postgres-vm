"""Auditable consolidation runs.

00-MASTER-BLUEPRINT §6.5 specifies the nightly consolidation workers as "each
idempotent, each writing an auditable `consolidation_runs` row". The audit row is
not bookkeeping: consolidation is the only unattended process that ARCHIVES
memories a human never asked it to touch, and dedup merges provenance across
rows. Without a durable record of what a pass examined and what it collapsed,
"why did this memory disappear" is unanswerable, and an over-eager threshold
would quietly erode the corpus with the evidence living only in a log line that
rotated away.

`details` carries the per-run specifics — the surviving id for each collapsed
group, the episode ids folded into a summary — so the console can walk backwards
from a summary memory to the originals it replaced.

Revision ID: 0031
Revises: 0029
"""
from alembic import op

revision = "0031"
down_revision = "0029"
branch_labels = None
depends_on = None


SQL = """
CREATE TABLE IF NOT EXISTS mem.consolidation_runs (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES mem.organizations(id) ON DELETE CASCADE,
    project_id        uuid NOT NULL REFERENCES mem.projects(id) ON DELETE CASCADE,
    kind              text NOT NULL CHECK (kind IN ('dedup', 'episode_compaction',
                                                    'procedure_distillation')),
    status            text NOT NULL DEFAULT 'completed'
                      CHECK (status IN ('completed', 'failed', 'skipped')),
    -- Examined vs affected, separately: a pass that looked at 4000 memories and
    -- collapsed 2 is healthy; one that collapsed 2000 is a threshold bug, and
    -- the two are indistinguishable from a single count.
    examined          integer NOT NULL DEFAULT 0 CHECK (examined >= 0),
    affected          integer NOT NULL DEFAULT 0 CHECK (affected >= 0),
    -- The thresholds IN FORCE for this run. They are configurable, so a run from
    -- last month cannot be interpreted against today's settings.
    parameters        jsonb NOT NULL DEFAULT '{}'::jsonb,
    details           jsonb NOT NULL DEFAULT '{}'::jsonb,
    error             text,
    started_at        timestamptz NOT NULL DEFAULT now(),
    completed_at      timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_consolidation_runs_project_time
  ON mem.consolidation_runs (tenant_id, project_id, kind, started_at DESC);

ALTER TABLE mem.consolidation_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE mem.consolidation_runs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS consolidation_runs_select ON mem.consolidation_runs;
CREATE POLICY consolidation_runs_select ON mem.consolidation_runs FOR SELECT
  USING (tenant_id = mem.current_tenant()
         AND project_id = ANY(mem.allowed_projects()));

DROP POLICY IF EXISTS consolidation_runs_insert ON mem.consolidation_runs;
CREATE POLICY consolidation_runs_insert ON mem.consolidation_runs FOR INSERT
  WITH CHECK (tenant_id = mem.current_tenant()
              AND project_id = nullif(current_setting('app.project_id', true), '')::uuid);

-- A run is opened before the work and closed after it, so the row has to be
-- updatable. Scoped the same way as the insert: a pass can only close its own
-- project's run.
DROP POLICY IF EXISTS consolidation_runs_update ON mem.consolidation_runs;
CREATE POLICY consolidation_runs_update ON mem.consolidation_runs FOR UPDATE
  USING (tenant_id = mem.current_tenant()
         AND project_id = nullif(current_setting('app.project_id', true), '')::uuid)
  WITH CHECK (tenant_id = mem.current_tenant()
              AND project_id = nullif(current_setting('app.project_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON mem.consolidation_runs TO memory_app;

-- Dedup collapses a group onto one survivor and has to record WHICH memories
-- were folded into it. mem.memory_supersessions already models exactly that edge
-- (new_id replaced old_id, with a reason), so consolidation reuses it rather
-- than inventing a parallel table that memory.explain would not know to walk.
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS mem.consolidation_runs;")
