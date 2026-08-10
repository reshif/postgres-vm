"""Close the cross-tenant leak through the derived/audit tables.

01-SCHEMA.sql enables RLS on the eight obvious tables and ends the list with
"... apply to every tenant-scoped table". Nine tenant-scoped tables were left
without RLS, and one of them is a full bypass of the isolation model:

    mem.fn_memory_version() is an AFTER trigger on mem.memories that writes
    to_jsonb(COALESCE(NEW, OLD)) — the ENTIRE row, content included — into
    mem.memory_versions, which had no RLS at all.

So mem.memories was correctly locked down while a complete copy of every
memory sat next door in the open. Demonstrated before writing this migration:
tenant B, in a properly scoped transaction, read tenant A's confidential
memory content out of memory_versions while `SELECT count(*) FROM mem.memories`
correctly returned 0.

This is precisely the leak ADR-0004 and the FORCE policy exist to prevent, and
the /readyz isolation self-test could never catch it: it asserts that an unscoped
read of mem.memories returns no rows, and that assertion stayed true the whole
time. A guard that watches one table does not protect the table it mirrors into.

Policies mirror the memories_read predicate wherever the row can be traced back
to a memory, so history cannot become a wider window than the thing it records.
memory_versions carries no project_id column, so it reads scope out of the
snapshot jsonb — slightly more expensive, but the alternative is tenant-only
granularity on a table that holds full memory bodies.

audit_log, ingestion_events and memory_versions get no UPDATE policy: they are
append-only records, and the app path should not be able to rewrite history it is
the subject of. scope_grants gets SELECT only — issuing a grant is an admin
operation, and a role that can widen its own scope has no scope.

NOT covered here, deliberately: mem.projects and mem.principals. They are
registry metadata rather than memory content, and project binding has to resolve
a project BEFORE a scope exists (05-BUILD-PLAN Phase 2), so RLS on them needs a
privileged resolution path designed alongside it. They are listed as explicit,
named exemptions in tests/test_rls_coverage.py so they stay visible rather than
quietly forgotten.

Revision ID: 0005
Revises: 0004
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

TABLES = [
    "memory_versions", "memory_supersessions", "audit_log",
    "ingestion_events", "proposed_relationships", "entity_aliases",
    "scope_grants",
]

ENABLE = "\n".join(
    f"ALTER TABLE mem.{t} ENABLE ROW LEVEL SECURITY;\n"
    f"ALTER TABLE mem.{t} FORCE  ROW LEVEL SECURITY;"
    for t in TABLES
)

POLICIES = """
-- memory_versions: full snapshots of memories. Scope is read out of the snapshot
-- because the row has no project_id of its own; the predicate deliberately
-- mirrors memories_read so history is never a wider window than the memory.
CREATE POLICY memory_versions_read ON mem.memory_versions FOR SELECT
USING (
  tenant_id = mem.current_tenant()
  AND (
        (snapshot->>'scope_kind') = 'organization'
    OR  (snapshot->>'project_id')::uuid = ANY (mem.allowed_projects())
    OR  (snapshot->>'owner_principal')::uuid
          = nullif(current_setting('app.principal_id', true), '')::uuid
  )
);

-- The trigger is not SECURITY DEFINER, so it inserts as the calling role and is
-- itself subject to this policy. Too strict here and every memory write fails.
CREATE POLICY memory_versions_write ON mem.memory_versions FOR INSERT
WITH CHECK (tenant_id = mem.current_tenant());

CREATE POLICY memory_supersessions_read ON mem.memory_supersessions FOR SELECT
USING (
  tenant_id = mem.current_tenant()
  AND EXISTS (SELECT 1 FROM mem.memories m WHERE m.id = memory_supersessions.new_id)
);

CREATE POLICY memory_supersessions_write ON mem.memory_supersessions FOR INSERT
WITH CHECK (
  tenant_id = mem.current_tenant()
  AND EXISTS (SELECT 1 FROM mem.memories m WHERE m.id = memory_supersessions.new_id)
);

CREATE POLICY audit_log_read ON mem.audit_log FOR SELECT
USING (tenant_id = mem.current_tenant());

CREATE POLICY audit_log_write ON mem.audit_log FOR INSERT
WITH CHECK (tenant_id = mem.current_tenant());

CREATE POLICY ingestion_events_read ON mem.ingestion_events FOR SELECT
USING (
  tenant_id = mem.current_tenant()
  AND (project_id IS NULL OR project_id = ANY (mem.allowed_projects()))
);

CREATE POLICY ingestion_events_write ON mem.ingestion_events FOR INSERT
WITH CHECK (
  tenant_id = mem.current_tenant()
  AND project_id = nullif(current_setting('app.project_id', true), '')::uuid
);

CREATE POLICY proposed_relationships_read ON mem.proposed_relationships FOR SELECT
USING (
  tenant_id = mem.current_tenant()
  AND (project_id IS NULL OR project_id = ANY (mem.allowed_projects()))
);

CREATE POLICY proposed_relationships_write ON mem.proposed_relationships FOR INSERT
WITH CHECK (
  tenant_id = mem.current_tenant()
  AND project_id = nullif(current_setting('app.project_id', true), '')::uuid
);

CREATE POLICY proposed_relationships_update ON mem.proposed_relationships FOR UPDATE
USING (
  tenant_id = mem.current_tenant()
  AND (project_id IS NULL OR project_id = ANY (mem.allowed_projects()))
)
WITH CHECK (tenant_id = mem.current_tenant());

CREATE POLICY entity_aliases_read ON mem.entity_aliases FOR SELECT
USING (
  tenant_id = mem.current_tenant()
  AND EXISTS (SELECT 1 FROM mem.entities e WHERE e.id = entity_aliases.entity_id)
);

CREATE POLICY entity_aliases_write ON mem.entity_aliases FOR INSERT
WITH CHECK (
  tenant_id = mem.current_tenant()
  AND EXISTS (SELECT 1 FROM mem.entities e WHERE e.id = entity_aliases.entity_id)
);

-- Read-only from the app path. A role that can grant itself scope has no scope.
CREATE POLICY scope_grants_read ON mem.scope_grants FOR SELECT
USING (tenant_id = mem.current_tenant());
"""

DISABLE = "\n".join(f"ALTER TABLE mem.{t} DISABLE ROW LEVEL SECURITY;" for t in TABLES)
DROP = "\n".join(
    f"DROP POLICY IF EXISTS {t}_read ON mem.{t};\n"
    f"DROP POLICY IF EXISTS {t}_write ON mem.{t};\n"
    f"DROP POLICY IF EXISTS {t}_update ON mem.{t};"
    for t in TABLES
)


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(ENABLE)
        cur.execute(POLICIES)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(DROP)
        cur.execute(DISABLE)
