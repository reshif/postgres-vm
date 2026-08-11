"""Partition retrieval_events by month.

05-BUILD-PLAN Phase 9: "partition `retrieval_events` monthly". It was one
unpartitioned table with one row per context pack and no eviction — measured at
~1.2 KB per row, so ~4.4 GB a year at 10k packs a day, growing forever on the
same volume as the database it is describing.

WHY PARTITIONING AND NOT A DELETE JOB. Dropping a partition is a catalogue
operation: instant, no dead tuples, no vacuum storm. `DELETE FROM
retrieval_events WHERE created_at < ...` on millions of rows produces exactly
the bloat and autovacuum pressure you were trying to avoid, on the table that is
also being written to on every request.

WHY THE DATA IS COPIED RATHER THAN LEFT BEHIND. `retrieval_events` is what
`/v1/explain?pack_id=` reads to answer "why did the agent say that yesterday",
and the Retrieval Debugger replays from it. Losing it would silently break
provenance for every pack recorded so far, and a debugging tool that quietly
forgot last week is worse than one that says it cannot help.

The rename-and-copy dance is deliberate: PostgreSQL cannot convert an existing
table to a partitioned one in place, so the only options are copy the rows or
discard them.

Revision ID: 0027
Revises: 0025
"""
from alembic import op

revision = "0027"
down_revision = "0025"
branch_labels = None
depends_on = None


SQL = """
-- Idempotent: if a previous attempt half-ran, do not stack another rename on it.
DO $$
DECLARE
    already boolean;
BEGIN
    SELECT c.relkind = 'p' INTO already
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'mem' AND c.relname = 'retrieval_events';
    IF already THEN
        RAISE NOTICE 'retrieval_events is already partitioned; nothing to do';
        RETURN;
    END IF;

    ALTER TABLE mem.retrieval_events RENAME TO retrieval_events_legacy;

    -- created_at joins the primary key because a partitioned table must include
    -- the partition key in every unique constraint — the index is per partition
    -- and cannot enforce uniqueness across them otherwise.
    CREATE TABLE mem.retrieval_events (
        LIKE mem.retrieval_events_legacy INCLUDING DEFAULTS INCLUDING CONSTRAINTS
    ) PARTITION BY RANGE (created_at);

    -- Twelve months back and three forward. Forward matters: a write with no
    -- matching partition FAILS, and that failure lands on the retrieval path.
    -- mem.ensure_retrieval_partitions() keeps the window rolling.
    PERFORM mem.ensure_retrieval_partitions();

    INSERT INTO mem.retrieval_events SELECT * FROM mem.retrieval_events_legacy;

    DROP TABLE mem.retrieval_events_legacy;
END $$;
"""

# Created before the DO block runs, because that block calls it.
FUNCTION_SQL = """
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
    -- A write with no partition is an ERROR, not a slow path, so the window is
    -- always extended ahead of now rather than on demand.
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
            created := created + 1;
        END IF;
    END LOOP;
    RETURN created;
END $$;

REVOKE ALL ON FUNCTION mem.ensure_retrieval_partitions(integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mem.ensure_retrieval_partitions(integer, integer) TO memory_app;

CREATE OR REPLACE FUNCTION mem.drop_old_retrieval_partitions(keep_months integer DEFAULT 12)
RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
    r record;
    dropped integer := 0;
    cutoff date := (date_trunc('month', now()) - make_interval(months => keep_months))::date;
BEGIN
    FOR r IN
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'mem' AND c.relname ~ '^retrieval_events_[0-9]{6}$'
    LOOP
        -- Parsed from the name rather than from pg_get_expr: the name IS the
        -- contract here, and reading it back is how the drop stays a catalogue
        -- operation instead of a scan.
        IF to_date(right(r.relname, 6), 'YYYYMM') < cutoff THEN
            EXECUTE format('DROP TABLE mem.%I', r.relname);
            dropped := dropped + 1;
        END IF;
    END LOOP;
    RETURN dropped;
END $$;

REVOKE ALL ON FUNCTION mem.drop_old_retrieval_partitions(integer) FROM PUBLIC;
"""

DOWN = """
-- Flatten back to one table. Data is preserved for the same reason the upgrade
-- preserves it: /v1/explain reads this.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname='mem' AND c.relname='retrieval_events' AND c.relkind='p') THEN
        CREATE TABLE mem.retrieval_events_flat (LIKE mem.retrieval_events INCLUDING ALL);
        INSERT INTO mem.retrieval_events_flat SELECT * FROM mem.retrieval_events;
        DROP TABLE mem.retrieval_events CASCADE;
        ALTER TABLE mem.retrieval_events_flat RENAME TO retrieval_events;
    END IF;
END $$;
DROP FUNCTION IF EXISTS mem.ensure_retrieval_partitions(integer, integer);
DROP FUNCTION IF EXISTS mem.drop_old_retrieval_partitions(integer);
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(FUNCTION_SQL)
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(DOWN)
