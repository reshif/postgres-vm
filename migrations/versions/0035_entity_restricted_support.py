"""Let the generalisation screen see restricted support it is not allowed to read.

ADR-0012 excludes `restricted` material from generalisation permanently. The
check for it was written as an ordinary count over `entity_mentions` joined to
`mem.memories` — and returned 0 every time, because the row-level policy added in
0023 hides restricted memories from a session without the matching grant.

So the safeguard was blind to precisely the material it exists to detect, and the
failure was silent and in the permissive direction: an entity backed only by
restricted content screened clean.

This function answers "is there restricted material behind this entity" without
disclosing any of it — the same shape as `mem.sensitivity_allowed`. It returns a
count and nothing else: no title, no content, no ids. A caller learns that
promotion is blocked, which is the one fact it needs, and learns nothing about
what is being protected.

Revision ID: 0035
Revises: 0033
"""
from alembic import op

revision = "0035"
down_revision = "0033"
branch_labels = None
depends_on = None


SQL = """
CREATE OR REPLACE FUNCTION mem.entity_restricted_support(p_entity_id uuid)
RETURNS integer
LANGUAGE sql
STABLE
SECURITY DEFINER
-- Pinned search_path: a SECURITY DEFINER function that resolves unqualified
-- names through the caller's search_path is the classic privilege-escalation
-- shape, and this one runs as the owner.
SET search_path = mem, pg_catalog
AS $$
    SELECT count(*)::integer
      FROM mem.entity_mentions em
      JOIN mem.memories m ON m.id = em.memory_id
      JOIN mem.entities e ON e.id = em.entity_id
     WHERE em.entity_id = p_entity_id
       -- Still tenant-bound. Bypassing the sensitivity policy is the point;
       -- bypassing tenant isolation is never the point.
       AND e.tenant_id = mem.current_tenant()
       AND m.tenant_id = mem.current_tenant()
       AND m.sensitivity = 'restricted';
$$;

REVOKE ALL ON FUNCTION mem.entity_restricted_support(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mem.entity_restricted_support(uuid) TO memory_app;
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute("DROP FUNCTION IF EXISTS mem.entity_restricted_support(uuid);")
