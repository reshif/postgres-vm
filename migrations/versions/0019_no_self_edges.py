"""Forbid self-edges in the knowledge graph.

`mem.relationships` contained `RRF uses RRF` and `RLS uses RLS`. They came from
the declared-relations path in entities.link_relations: a document's most-weighted
entity is used as the subject for any `relates:` entry in its frontmatter, and an
ADR that discusses exactly one technology and declares a relation to it names the
same entity on both sides.

WHY A CONSTRAINT AND NOT ONLY THE CODE FIX. A self-edge is never information, and
it actively degrades the graph arm: expansion walks from a seed entity to its
neighbours, so a loop makes an entity its own neighbour and inflates its apparent
connectivity. The application guard is in place, but extraction is the part of
this system most likely to be rewritten — a rule that only lives in Python is a
rule that survives until someone writes a second extractor.

The cleanup runs first: the constraint cannot be added while the rows it forbids
are still there, and failing the migration on existing data would leave a
half-applied schema on every existing deployment.

Revision ID: 0019
Revises: 0018
"""
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


SQL = """
DELETE FROM mem.relationships           WHERE source_id = target_id;
DELETE FROM mem.proposed_relationships  WHERE source_id = target_id;

ALTER TABLE mem.relationships
  DROP CONSTRAINT IF EXISTS relationships_no_self_edge;
ALTER TABLE mem.relationships
  ADD CONSTRAINT relationships_no_self_edge CHECK (source_id <> target_id);

ALTER TABLE mem.proposed_relationships
  DROP CONSTRAINT IF EXISTS proposed_relationships_no_self_edge;
ALTER TABLE mem.proposed_relationships
  ADD CONSTRAINT proposed_relationships_no_self_edge CHECK (source_id <> target_id);
"""

DOWN = """
ALTER TABLE mem.relationships          DROP CONSTRAINT IF EXISTS relationships_no_self_edge;
ALTER TABLE mem.proposed_relationships DROP CONSTRAINT IF EXISTS proposed_relationships_no_self_edge;
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(DOWN)
