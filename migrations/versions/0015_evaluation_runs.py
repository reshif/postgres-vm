"""Persist evaluation runs and per-case evidence for the console.

The markdown report remains the reviewed narrative, but an Evals view cannot
compare a run from Tuesday to one from today if the only structured result dies
with the CI log. These rows are append-only evidence: a run records the corpus,
profile, metrics, and every case result that produced its outcome.

Revision ID: 0015
Revises: 0014
"""
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


SQL = """
CREATE TABLE IF NOT EXISTS mem.evaluation_runs (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES mem.organizations(id) ON DELETE CASCADE,
    project_id        uuid NOT NULL REFERENCES mem.projects(id) ON DELETE CASCADE,
    suite             text NOT NULL CHECK (length(trim(suite)) BETWEEN 1 AND 100),
    status            text NOT NULL CHECK (status IN ('passed', 'failed', 'incomplete')),
    corpus_snapshot   text NOT NULL DEFAULT '',
    ranking_profile   text,
    source_commit     text,
    metrics           jsonb NOT NULL DEFAULT '{}'::jsonb,
    configuration     jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at        timestamptz NOT NULL DEFAULT now(),
    completed_at      timestamptz,
    created_by        uuid REFERENCES mem.principals(id),
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_evaluation_runs_project_time
  ON mem.evaluation_runs (tenant_id, project_id, suite, completed_at DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS mem.evaluation_case_results (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id            uuid NOT NULL REFERENCES mem.evaluation_runs(id) ON DELETE CASCADE,
    tenant_id         uuid NOT NULL REFERENCES mem.organizations(id) ON DELETE CASCADE,
    project_id        uuid NOT NULL REFERENCES mem.projects(id) ON DELETE CASCADE,
    case_id           text NOT NULL CHECK (length(trim(case_id)) BETWEEN 1 AND 200),
    query_text        text NOT NULL,
    status            text NOT NULL CHECK (status IN ('passed', 'failed', 'incomplete')),
    result            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT evaluation_case_result_unique UNIQUE (run_id, case_id)
);

CREATE INDEX IF NOT EXISTS idx_evaluation_case_results_run
  ON mem.evaluation_case_results (tenant_id, project_id, run_id, case_id);

ALTER TABLE mem.evaluation_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE mem.evaluation_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE mem.evaluation_case_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE mem.evaluation_case_results FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS evaluation_runs_select ON mem.evaluation_runs;
CREATE POLICY evaluation_runs_select ON mem.evaluation_runs FOR SELECT
  USING (tenant_id = mem.current_tenant()
         AND project_id = ANY(mem.allowed_projects()));
DROP POLICY IF EXISTS evaluation_runs_insert ON mem.evaluation_runs;
CREATE POLICY evaluation_runs_insert ON mem.evaluation_runs FOR INSERT
  WITH CHECK (tenant_id = mem.current_tenant()
              AND project_id = nullif(current_setting('app.project_id', true), '')::uuid);

DROP POLICY IF EXISTS evaluation_case_results_select ON mem.evaluation_case_results;
CREATE POLICY evaluation_case_results_select ON mem.evaluation_case_results FOR SELECT
  USING (tenant_id = mem.current_tenant()
         AND project_id = ANY(mem.allowed_projects()));
DROP POLICY IF EXISTS evaluation_case_results_insert ON mem.evaluation_case_results;
CREATE POLICY evaluation_case_results_insert ON mem.evaluation_case_results FOR INSERT
  WITH CHECK (tenant_id = mem.current_tenant()
              AND project_id = nullif(current_setting('app.project_id', true), '')::uuid);

GRANT SELECT, INSERT ON mem.evaluation_runs, mem.evaluation_case_results TO memory_app;
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS mem.evaluation_case_results;")
        cur.execute("DROP TABLE IF EXISTS mem.evaluation_runs;")
