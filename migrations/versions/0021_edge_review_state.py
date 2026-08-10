"""Give proposed relationships a review outcome.

`mem.proposed_relationships` had `reviewed_by` and nothing else — no record of
WHAT was decided. That is enough to know a human looked, and not enough to keep a
rejected edge from being re-proposed by the next extraction pass and re-reviewed
forever. Rejection has to be durable or the inbox becomes a treadmill, which is
precisely the curation-capacity failure ADR-0015 is about.

Three columns, matching the vocabulary the memory review path already uses
(`inbox.promote` / `inbox.reject`):

  decision       accepted | rejected, NULL while pending
  reviewed_at    when, so the inbox can order by age and report a backlog
  review_reason  why, because a rejection without a reason teaches nobody

WHY NOT DELETE A REJECTED PROPOSAL. Same rule as memories: rejection archives, it
does not erase. "Did we already consider linking these two?" must stay
answerable, and a deleted row cannot answer it.

Revision ID: 0021
Revises: 0019
"""
from alembic import op

revision = "0021"
down_revision = "0019"
branch_labels = None
depends_on = None


SQL = """
ALTER TABLE mem.proposed_relationships
  ADD COLUMN IF NOT EXISTS decision      text,
  ADD COLUMN IF NOT EXISTS reviewed_at   timestamptz,
  ADD COLUMN IF NOT EXISTS review_reason text;

ALTER TABLE mem.proposed_relationships
  DROP CONSTRAINT IF EXISTS proposed_relationships_decision_check;
ALTER TABLE mem.proposed_relationships
  ADD CONSTRAINT proposed_relationships_decision_check
  CHECK (decision IS NULL OR decision IN ('accepted', 'rejected'));

-- The inbox reads pending proposals on every listing, and the vast majority of
-- rows become reviewed. A partial index keeps that query proportional to the
-- size of the QUEUE rather than to every edge ever proposed.
CREATE INDEX IF NOT EXISTS idx_proposed_pending
  ON mem.proposed_relationships (tenant_id, project_id, proposed_at)
  WHERE decision IS NULL;
"""

DOWN = """
DROP INDEX IF EXISTS mem.idx_proposed_pending;
ALTER TABLE mem.proposed_relationships
  DROP CONSTRAINT IF EXISTS proposed_relationships_decision_check;
ALTER TABLE mem.proposed_relationships
  DROP COLUMN IF EXISTS decision,
  DROP COLUMN IF EXISTS reviewed_at,
  DROP COLUMN IF EXISTS review_reason;
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(DOWN)
