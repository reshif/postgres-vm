"""Relocate the procrastinate queue objects from mem back to public.

THE BUG. 01-SCHEMA.sql line 30 contains a bare `SET search_path = mem, public`.
migrations/env.py wraps the whole upgrade in ONE transaction
(`with context.begin_transaction(): context.run_migrations()`), so that SET is
session-scoped and survives past migration 0001. Migration 0002 then applies
procrastinate's schema, which uses unqualified names, and all 39 tables,
functions and types are created in `mem` instead of `public`.

WHY IT HID FOR SO LONG. It only reproduces on a FRESH database, where 0001 and
0002 run in the same session. On an incremental upgrade — 0002 applied on its own
against an already-migrated database — the search_path is the default and the
objects land in `public` correctly. So the queue worked for as long as this
database was upgraded incrementally and broke the first time the volume was
wiped and rebuilt from scratch. A migration that produces a different schema on a
fresh install than on an upgrade is the worst kind, because CI and production
disagree and alembic reports success in both.

THE SYMPTOM. The worker crash-loops on
`UndefinedFunction: procrastinate_prune_stalled_workers_v1(double precision)
does not exist`, because it connects with the default search_path and cannot see
`mem`. alembic_version says 0009 and every object it names is present, just in
the wrong schema — so nothing looks wrong until the worker runs.

THE FIX, in two parts:
  * This migration drops any procrastinate objects found in `mem` and recreates
    them in `public`. Dropping is safe here and is checked for: it refuses if the
    queue holds jobs, because silently discarding queued work to fix a schema
    mistake would be a much worse bug than the one being fixed.
  * Migration 0002 now pins `SET LOCAL search_path = public` before applying, so
    a fresh install never reproduces this and this migration becomes a no-op.

Revision ID: 0010
Revises: 0009
"""
from alembic import op
from procrastinate.schema import SchemaManager

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


COUNT_IN = """
SELECT (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND c.relname LIKE 'procrastinate%%' AND c.relkind = 'r')
     + (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = %s AND p.proname LIKE 'procrastinate%%')
"""

DROP_FROM_MEM = """
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'mem' AND c.relname LIKE 'procrastinate%' AND c.relkind = 'r'
  LOOP EXECUTE format('DROP TABLE IF EXISTS mem.%I CASCADE', r.relname); END LOOP;

  FOR r IN SELECT p.oid::regprocedure AS sig FROM pg_proc p
             JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'mem' AND p.proname LIKE 'procrastinate%'
  LOOP EXECUTE format('DROP FUNCTION IF EXISTS %s CASCADE', r.sig); END LOOP;

  FOR r IN SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname = 'mem' AND t.typname LIKE 'procrastinate%'
  LOOP EXECUTE format('DROP TYPE IF EXISTS mem.%I CASCADE', r.typname); END LOOP;
END $$;
"""

GRANTS = """
GRANT USAGE ON SCHEMA public TO memory_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO memory_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO memory_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO memory_app;
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(COUNT_IN, ("public", "public"))
        in_public = cur.fetchone()[0]
        cur.execute(COUNT_IN, ("mem", "mem"))
        in_mem = cur.fetchone()[0]

        if in_public and not in_mem:
            return  # already correct

        if in_mem:
            # Refuse to discard queued work. If this fires, drain the queue (or
            # dump mem.procrastinate_jobs) before upgrading — the schema mistake
            # is recoverable, the jobs are not.
            cur.execute(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                " WHERE n.nspname = 'mem' AND c.relname = 'procrastinate_jobs'"
            )
            if cur.fetchone()[0]:
                cur.execute("SELECT count(*) FROM mem.procrastinate_jobs")
                pending = cur.fetchone()[0]
                if pending:
                    raise RuntimeError(
                        f"mem.procrastinate_jobs holds {pending} job(s). This migration "
                        "moves the queue schema to `public` and will not silently drop "
                        "queued work. Drain or dump the queue, then re-run."
                    )
            cur.execute(DROP_FROM_MEM)

        # Pin the target schema explicitly. Relying on the ambient search_path is
        # exactly what caused this.
        cur.execute("SET LOCAL search_path = public")
        cur.execute(SchemaManager.get_schema())
        cur.execute(GRANTS)


def downgrade() -> None:
    # Deliberately not reversible: moving the queue back into `mem` would restore
    # a broken worker. Roll forward.
    pass
