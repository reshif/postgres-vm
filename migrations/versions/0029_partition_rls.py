"""Restore RLS on retrieval_events and put it on every partition.

MIGRATION 0027 STRIPPED IT. Converting the table to a partitioned one meant
recreating it, and `CREATE TABLE ... (LIKE source INCLUDING DEFAULTS INCLUDING
CONSTRAINTS)` copies neither row-level security nor policies. There is no
INCLUDING clause that does. So a table holding per-tenant query text and pack
contents lost every policy it had, silently, in a migration whose stated purpose
was disk management.

`tests/test_rls_coverage.py` caught it — 40/72 — which is the entire argument for
enumerating tables rather than listing them.

PARTITIONS NEED THEIR OWN. Policies on a partitioned parent apply to queries
routed THROUGH the parent. A query naming a partition directly
(`SELECT * FROM mem.retrieval_events_202601`) is governed by that partition's own
policies, and a fresh partition has none. Since `ensure_retrieval_partitions()`
creates tables at runtime, every future partition would arrive unprotected — so
the function has to secure what it creates, not just create it.

The policies are exactly those migration 0003 applied: project-scoped read,
scoped insert, and no UPDATE, because a retrieval log is append-only.

Revision ID: 0029
Revises: 0027
"""
from alembic import op

revision = "0029"
down_revision = "0027"
branch_labels = None
depends_on = None


SQL = """
-- One function so the parent and every partition, present and future, get the
-- identical predicate. Two copies of a security rule drift, and the drift is
-- invisible until someone queries the half that was forgotten.
CREATE OR REPLACE FUNCTION mem.secure_retrieval_table(tbl regclass)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', tbl);
    EXECUTE format('ALTER TABLE %s FORCE  ROW LEVEL SECURITY', tbl);

    EXECUTE format('DROP POLICY IF EXISTS retrieval_events_read ON %s', tbl);
    EXECUTE format($f$
        CREATE POLICY retrieval_events_read ON %s FOR SELECT
        USING (
            tenant_id = mem.current_tenant()
            AND (project_id IS NULL OR project_id = ANY (mem.allowed_projects()))
        )$f$, tbl);

    EXECUTE format('DROP POLICY IF EXISTS retrieval_events_write ON %s', tbl);
    EXECUTE format($f$
        CREATE POLICY retrieval_events_write ON %s FOR INSERT
        WITH CHECK (
            tenant_id = mem.current_tenant()
            AND project_id = nullif(current_setting('app.project_id', true), '')::uuid
        )$f$, tbl);
    -- No UPDATE policy, deliberately: a retrieval log is append-only, and
    -- /v1/explain answers "why did the agent say that" from what was RECORDED.
    -- A rewritable log cannot answer that question honestly.
END $$;

-- The parent, plus every partition that already exists.
SELECT mem.secure_retrieval_table('mem.retrieval_events'::regclass);

DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT c.oid::regclass AS t
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'mem' AND c.relname ~ '^retrieval_events_[0-9]{6}$'
    LOOP
        PERFORM mem.secure_retrieval_table(r.t);
    END LOOP;
END $$;

-- And every partition created from now on. Without this, next month's partition
-- appears unprotected and nothing notices until the coverage suite runs.
CREATE OR REPLACE FUNCTION mem.ensure_retrieval_partitions(
    back_months integer DEFAULT 12,
    ahead_months integer DEFAULT 3
) RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
    m date;
    created integer := 0;
    part text;
BEGIN
    FOR m IN
        SELECT generate_series(
            date_trunc('month', now()) - make_interval(months => back_months),
            date_trunc('month', now()) + make_interval(months => ahead_months),
            interval '1 month')::date
    LOOP
        part := 'retrieval_events_' || to_char(m, 'YYYYMM');
        IF NOT EXISTS (
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'mem' AND c.relname = part
        ) THEN
            EXECUTE format(
                'CREATE TABLE mem.%I PARTITION OF mem.retrieval_events '
                'FOR VALUES FROM (%L) TO (%L)',
                part, m, (m + interval '1 month')::date);
            -- Secured in the same statement that creates it. A partition that
            -- exists unprotected for even one maintenance cycle is a hole.
            PERFORM mem.secure_retrieval_table(format('mem.%I', part)::regclass);
            created := created + 1;
        END IF;
    END LOOP;
    RETURN created;
END $$;

REVOKE ALL ON FUNCTION mem.ensure_retrieval_partitions(integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mem.ensure_retrieval_partitions(integer, integer) TO memory_app;
GRANT SELECT, INSERT ON mem.retrieval_events TO memory_app;
"""

DOWN = """
DROP FUNCTION IF EXISTS mem.secure_retrieval_table(regclass);
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(DOWN)
