"""Durable task handles for the MCP Tasks extension.

02-MCP-CONTRACT.md: "Tasks extension (io.modelcontextprotocol/tasks) for long
operations: repository ingestion, re-embedding, consolidation runs, evaluation
runs. Poll with tasks/get; accept input with tasks/update."

The shape of this table is dictated by ADR-0004 rather than by convenience. That
ADR refuses to lock "the system is stateless" and instead permits application
state that is:

    explicit    — a server-minted handle passed as an ordinary argument, never
                  implied by the connection
    durable     — stored in PostgreSQL, not in process memory
    attributable— bound to a principal and a scope, and auditable

A dict in the gateway process would satisfy none of those. It would also be
wrong in a way that only shows up in production: the gateway is horizontally
scaled, so the poll for a task would reach a replica that never saw it created.

`request_state` carries the MRTR correlation token, so a task that needs
confirmation mid-flight (tasks/update with inputResponses) can be matched to the
question it was asked — the same mechanism the synchronous confirmations use.

Revision ID: 0037
Revises: 0035
"""
from alembic import op

revision = "0037"
down_revision = "0035"
branch_labels = None
depends_on = None


SQL = """
CREATE TABLE IF NOT EXISTS mem.mcp_tasks (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES mem.organizations(id) ON DELETE CASCADE,
    project_id    uuid NOT NULL REFERENCES mem.projects(id) ON DELETE CASCADE,
    principal_id  uuid REFERENCES mem.principals(id),
    kind          text NOT NULL CHECK (kind IN ('ingest', 'reembed', 'consolidate',
                                                'evaluate')),
    -- The extension's own vocabulary. `input_required` is the state that makes
    -- tasks/update meaningful: the task is alive and waiting on the client.
    status        text NOT NULL DEFAULT 'working'
                  CHECK (status IN ('working', 'input_required', 'completed',
                                    'failed', 'cancelled')),
    arguments     jsonb NOT NULL DEFAULT '{}'::jsonb,
    result        jsonb,
    error         text,
    -- MRTR correlation, shared with the synchronous confirmation path.
    request_state text,
    input_request jsonb,
    progress      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    completed_at  timestamptz
);

CREATE INDEX IF NOT EXISTS idx_mcp_tasks_scope
  ON mem.mcp_tasks (tenant_id, project_id, status, created_at DESC);

ALTER TABLE mem.mcp_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE mem.mcp_tasks FORCE ROW LEVEL SECURITY;

-- A task handle is a capability: holding the id is how a client polls. It is
-- still scope-bound, so a leaked handle does not read across a tenant.
DROP POLICY IF EXISTS mcp_tasks_select ON mem.mcp_tasks;
CREATE POLICY mcp_tasks_select ON mem.mcp_tasks FOR SELECT
  USING (tenant_id = mem.current_tenant()
         AND project_id = ANY(mem.allowed_projects()));

DROP POLICY IF EXISTS mcp_tasks_insert ON mem.mcp_tasks;
CREATE POLICY mcp_tasks_insert ON mem.mcp_tasks FOR INSERT
  WITH CHECK (tenant_id = mem.current_tenant()
              AND project_id = nullif(current_setting('app.project_id', true), '')::uuid);

DROP POLICY IF EXISTS mcp_tasks_update ON mem.mcp_tasks;
CREATE POLICY mcp_tasks_update ON mem.mcp_tasks FOR UPDATE
  USING (tenant_id = mem.current_tenant()
         AND project_id = nullif(current_setting('app.project_id', true), '')::uuid)
  WITH CHECK (tenant_id = mem.current_tenant()
              AND project_id = nullif(current_setting('app.project_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON mem.mcp_tasks TO memory_app;
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS mem.mcp_tasks;")
