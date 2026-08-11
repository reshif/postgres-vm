"""Distinguish source-repository and sidecar-evidence deliveries.

Both repositories are bound to one project but they have different effects:
source events create immutable technical evidence, while an evidence-repository
event can advance reviewed assertion state. Recording the role at receipt makes
that distinction auditable and prevents a source-repository push from being
mistaken for a curator-approved assertion update.

Revision ID: 0041
Revises: 0039
"""
from alembic import op

revision = "0041"
down_revision = "0039"
branch_labels = None
depends_on = None


SQL = """
ALTER TABLE mem.github_deliveries
  ADD COLUMN repository_role text NOT NULL DEFAULT 'source';
ALTER TABLE mem.github_deliveries
  ADD CONSTRAINT github_deliveries_repository_role_check
  CHECK (repository_role IN ('source', 'evidence'));
CREATE INDEX idx_github_deliveries_role_status
  ON mem.github_deliveries (tenant_id, project_id, repository_role, status, received_at DESC);
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute("""
        ALTER TABLE mem.github_deliveries
          DROP CONSTRAINT IF EXISTS github_deliveries_repository_role_check;
        ALTER TABLE mem.github_deliveries
          DROP COLUMN IF EXISTS repository_role;
        """)
