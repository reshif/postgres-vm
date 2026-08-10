"""Project-scoped saved console views.

The Knowledge Explorer's filters are URL state so one view can be copied or
bookmarked. A useful operator surface also needs named project views such as
``Contested`` and ``Never retrieved`` without smuggling that state into browser
local storage, where it cannot be shared or audited.

Revision ID: 0014
Revises: 0013
"""
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


SQL = """
CREATE TABLE IF NOT EXISTS mem.saved_views (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES mem.organizations(id) ON DELETE CASCADE,
    project_id  uuid NOT NULL REFERENCES mem.projects(id) ON DELETE CASCADE,
    name        text NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 100),
    filters     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by  uuid REFERENCES mem.principals(id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT saved_views_project_name UNIQUE (project_id, name)
);

CREATE INDEX IF NOT EXISTS idx_saved_views_project
  ON mem.saved_views (tenant_id, project_id, name);

ALTER TABLE mem.saved_views ENABLE ROW LEVEL SECURITY;
ALTER TABLE mem.saved_views FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS saved_views_select ON mem.saved_views;
CREATE POLICY saved_views_select ON mem.saved_views FOR SELECT
  USING (tenant_id = mem.current_tenant()
         AND project_id = ANY(mem.allowed_projects()));

DROP POLICY IF EXISTS saved_views_insert ON mem.saved_views;
CREATE POLICY saved_views_insert ON mem.saved_views FOR INSERT
  WITH CHECK (tenant_id = mem.current_tenant()
              AND project_id = nullif(current_setting('app.project_id', true), '')::uuid);

DROP POLICY IF EXISTS saved_views_update ON mem.saved_views;
CREATE POLICY saved_views_update ON mem.saved_views FOR UPDATE
  USING (tenant_id = mem.current_tenant()
         AND project_id = nullif(current_setting('app.project_id', true), '')::uuid)
  WITH CHECK (tenant_id = mem.current_tenant()
              AND project_id = nullif(current_setting('app.project_id', true), '')::uuid);

DROP POLICY IF EXISTS saved_views_delete ON mem.saved_views;
CREATE POLICY saved_views_delete ON mem.saved_views FOR DELETE
  USING (tenant_id = mem.current_tenant()
         AND project_id = nullif(current_setting('app.project_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON mem.saved_views TO memory_app;
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS mem.saved_views;")
