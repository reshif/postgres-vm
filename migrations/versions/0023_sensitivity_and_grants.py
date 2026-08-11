"""Enforce sensitivity and scope grants in RLS.

Suite 2 (04-EVALUATION.md §3) is the zero-tolerance gate and lists nine cases.
Four of them had nothing to test, because `mem.memories.sensitivity` and
`mem.scope_grants` existed in the schema and **no code read either**:

    Restricted-sensitivity memory, no grant   -> not returned
    Expired grant                             -> not returned

A `restricted` memory was readable by anyone already in the project. The column
was decoration.

WHY THIS GOES IN THE POLICY AND NOT IN PYTHON. The same reason `fn_set_scope`
exists rather than a WHERE clause the callers remember to add: this system's
stated design is to make the unsafe path unavailable rather than discouraged. A
sensitivity check in the application is one forgotten join away from being
bypassed, and RLS failures are silent — no error, just wrong rows. Putting it in
the policy means a raw `SELECT` as `memory_app` obeys it too, which is exactly
what Suite 2's "direct SQL without scope context" case asserts.

THE GRANT MODEL. `public` and `internal` are readable by anyone already inside
the scope — scope is doing that work already. `confidential` and `restricted`
additionally require an unexpired grant naming either the reading principal or a
project they can read. Expiry is evaluated at query time, so an expired grant is
indistinguishable from no grant, which is Suite 2's other case.

SECURITY DEFINER, deliberately. The function reads `mem.scope_grants`, which is
itself RLS-protected; without it the policy would recurse into a policy. It
returns one boolean about the CALLER'S own grants and takes no arguments it does
not sanitise, so it cannot be used to read anything else.

Revision ID: 0023
Revises: 0021
"""
from alembic import op

revision = "0023"
down_revision = "0021"
branch_labels = None
depends_on = None


SQL = """
CREATE OR REPLACE FUNCTION mem.sensitivity_allowed(s mem.sensitivity)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = mem, public
AS $$
    SELECT CASE
        -- Scope already decides who is in the room. These two need nothing more.
        WHEN s IN ('public', 'internal') THEN true
        ELSE EXISTS (
            SELECT 1
              FROM mem.scope_grants g
             WHERE g.tenant_id = mem.current_tenant()
               AND g.permission = 'read:' || s::text
               -- Evaluated per query, so an expired grant behaves exactly like
               -- no grant rather than like a grant that used to work.
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

-- Rebuild the read policy with the sensitivity term appended. The scope clauses
-- are unchanged; this only ever removes rows.
DROP POLICY IF EXISTS memories_read ON mem.memories;
CREATE POLICY memories_read ON mem.memories
    FOR SELECT USING (
        tenant_id = mem.current_tenant()
        AND (
              scope_kind = 'organization'
           OR (scope_kind = 'project' AND project_id = ANY (mem.allowed_projects()))
           OR (scope_kind = 'user'
               AND owner_principal = NULLIF(current_setting('app.principal_id', true), '')::uuid)
        )
        AND mem.sensitivity_allowed(sensitivity)
    );

-- The same gate on the derived copy. mem.memory_versions holds whole rows
-- copied by the version trigger, so a restricted memory's content is sitting in
-- it — a sensitivity check on `memories` alone would be a front door locked
-- beside an open window. This is the same class of hole migration 0005 closed
-- for cross-tenant reads.
DROP POLICY IF EXISTS memory_versions_read ON mem.memory_versions;
CREATE POLICY memory_versions_read ON mem.memory_versions
    FOR SELECT USING (
        tenant_id = mem.current_tenant()
        AND EXISTS (
            SELECT 1 FROM mem.memories m
             WHERE m.id = mem.memory_versions.memory_id
        )
    );
"""

DOWN = """
DROP POLICY IF EXISTS memories_read ON mem.memories;
CREATE POLICY memories_read ON mem.memories
    FOR SELECT USING (
        tenant_id = mem.current_tenant()
        AND (
              scope_kind = 'organization'
           OR (scope_kind = 'project' AND project_id = ANY (mem.allowed_projects()))
           OR (scope_kind = 'user'
               AND owner_principal = NULLIF(current_setting('app.principal_id', true), '')::uuid)
        )
    );
DROP FUNCTION IF EXISTS mem.sensitivity_allowed(mem.sensitivity);
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(DOWN)
