"""Allow UPDATE on mem.ingestion_events — correcting an over-tight policy in 0005.

0005 gave ingestion_events SELECT and INSERT only, grouping it with audit_log and
memory_versions as "append-only records the app should not rewrite". That was
wrong about this table specifically, for two reasons visible in the schema itself:

  * It carries `observed_at` AND `processed_at`. Two timestamps for one row only
    make sense if the row is expected to change state as the file moves through
    the pipeline — observed, then processed.

  * It has UNIQUE (tenant_id, source_uri, content_hash). That constraint is what
    makes a poll loop safe: re-seeing identical bytes must not append a row. The
    natural expression is ON CONFLICT ... DO UPDATE, which requires an UPDATE
    policy. Without one, ingestion fails on the SECOND run with
    `new row violates row-level security policy (USING expression)` — a message
    that points at RLS while the actual cause is an upsert with nowhere to go.

audit_log and memory_versions stay append-only. The distinction is real: those
record what happened and must not be editable by the thing they describe.
ingestion_events records the current reconciliation state of a file, which is a
different kind of row.

Revision ID: 0006
Revises: 0005
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

SQL = """
CREATE POLICY ingestion_events_update ON mem.ingestion_events FOR UPDATE
USING (
  tenant_id = mem.current_tenant()
  AND (project_id IS NULL OR project_id = ANY (mem.allowed_projects()))
)
WITH CHECK (
  tenant_id = mem.current_tenant()
  AND (project_id IS NULL OR project_id = ANY (mem.allowed_projects()))
);
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute("DROP POLICY IF EXISTS ingestion_events_update ON mem.ingestion_events;")
