"""Make memory uniqueness match memory VISIBILITY.

THE BUG. `memories_temporal_uniq` was UNIQUE (tenant_id, memory_key, valid_at)
— tenant-wide. But a memory is not visible tenant-wide: `memories_read` scopes
reads by `scope_kind`, so a project memory is visible only inside its project.

The write path deduplicates by looking for the same content_hash first, and that
lookup runs under RLS. So for two projects in one tenant holding identical
content:

    project A: writes "ADR-0001: use Postgres"        -> ok
    project B: writes "ADR-0001: use Postgres"
               dedup SELECT ....................... 0 rows (RLS hides A's row)
               INSERT ............................. ExclusionViolation

The write fails with a raw constraint error naming an object the caller cannot
see and cannot query. Two ordinary situations reach it: a tenant with two
projects that share a convention file, and any monorepo where the same
`.memory/` path exists in two registered projects (ingest derives memory_key
from the path, so the keys collide exactly).

THE FIX. The uniqueness domain should equal the visibility domain. `scope_key` is
generated to mirror `memories_read` clause for clause: organization memories are
unique per tenant, project memories per project, user memories per owner. Then a
row can only collide with a row the writer could actually have found.

This is strictly weaker than the old constraint — it adds a column to the key —
so no existing row can violate it, and supersession is unaffected: the lookup it
uses was already RLS-scoped, and now the constraint agrees with it.

Revision ID: 0013
Revises: 0012
"""
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


SQL = """
-- Mirrors mem.memories_read clause for clause. COALESCE to tenant_id is a
-- backstop for a row whose discriminating column is NULL: without it scope_key
-- would be NULL, and NULLs are exempt from UNIQUE — the constraint would
-- silently stop applying to exactly the malformed rows that most need it.
ALTER TABLE mem.memories
  ADD COLUMN IF NOT EXISTS scope_key uuid
  GENERATED ALWAYS AS (
    COALESCE(
      CASE scope_kind
        WHEN 'organization' THEN tenant_id
        WHEN 'project'      THEN project_id
        WHEN 'user'         THEN owner_principal
      END,
      tenant_id)
  ) STORED;

ALTER TABLE mem.memories DROP CONSTRAINT IF EXISTS memories_temporal_uniq;
ALTER TABLE mem.memories
  ADD CONSTRAINT memories_temporal_uniq
  UNIQUE (tenant_id, scope_key, memory_key, valid_at WITHOUT OVERLAPS);
"""

DOWN = """
ALTER TABLE mem.memories DROP CONSTRAINT IF EXISTS memories_temporal_uniq;
ALTER TABLE mem.memories
  ADD CONSTRAINT memories_temporal_uniq
  UNIQUE (tenant_id, memory_key, valid_at WITHOUT OVERLAPS);
ALTER TABLE mem.memories DROP COLUMN IF EXISTS scope_key;
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(DOWN)
