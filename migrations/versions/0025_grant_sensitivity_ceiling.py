"""Express the grant's sensitivity ceiling as a level, not a verb.

Migration 0023 checked for `permission = 'read:restricted'`. That form cannot
exist: `mem.scope_grants` has carried

    CHECK (permission = ANY (ARRAY['read', 'write', 'promote']))

since the original schema, so the INSERT was rejected and no grant could ever
satisfy the policy — every confidential and restricted memory was unreadable by
everyone, permanently. Failing closed is the right direction to fail, but it is
still wrong.

The correct model was already implied by the schema: a grant says WHAT someone
may do (`read`), and sensitivity says HOW FAR that reach goes. Those are two
dimensions, so they get two columns rather than a compound string.

`max_sensitivity` NULL means the grant reaches `internal` — the level scope
alone already permits — so existing grants keep exactly the meaning they had.
The enum is declared public < internal < confidential < restricted, and Postgres
compares enums by declaration order, so `>=` is the ceiling test.

Revision ID: 0025
Revises: 0023
"""
from alembic import op

revision = "0025"
down_revision = "0023"
branch_labels = None
depends_on = None


SQL = """
ALTER TABLE mem.scope_grants
  ADD COLUMN IF NOT EXISTS max_sensitivity mem.sensitivity;

COMMENT ON COLUMN mem.scope_grants.max_sensitivity IS
  'Highest sensitivity this grant reaches. NULL means internal, i.e. no more '
  'than scope already allows.';

CREATE OR REPLACE FUNCTION mem.sensitivity_allowed(s mem.sensitivity)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = mem, public
AS $$
    SELECT CASE
        WHEN s IN ('public', 'internal') THEN true
        ELSE EXISTS (
            SELECT 1
              FROM mem.scope_grants g
             WHERE g.tenant_id = mem.current_tenant()
               AND g.permission = 'read'
               -- The ceiling. A grant to `confidential` does not open
               -- `restricted`; enum declaration order makes that comparison
               -- mean what it reads like.
               AND g.max_sensitivity IS NOT NULL
               AND g.max_sensitivity >= s
               -- Checked per query, so an expired grant is indistinguishable
               -- from no grant rather than from a grant that used to work.
               AND (g.expires_at IS NULL OR g.expires_at > now())
               AND (
                     (g.to_kind = 'user'
                      AND g.to_id = NULLIF(current_setting('app.principal_id', true), '')::uuid)
                  OR (g.to_kind = 'project'
                      AND g.to_id = ANY (mem.allowed_projects()))
                  OR (g.to_kind = 'organization'
                      AND g.to_id = mem.current_tenant())
               )
        )
    END;
$$;

REVOKE ALL ON FUNCTION mem.sensitivity_allowed(mem.sensitivity) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mem.sensitivity_allowed(mem.sensitivity)
  TO memory_app, memory_ro;
"""

DOWN = """
ALTER TABLE mem.scope_grants DROP COLUMN IF EXISTS max_sensitivity;
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(DOWN)
