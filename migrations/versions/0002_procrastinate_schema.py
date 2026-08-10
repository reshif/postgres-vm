"""Procrastinate queue schema + grants for memory_app.

The queue lives in Postgres (no Redis), so Procrastinate's own tables and
functions are part of this database's schema and belong under the same
`alembic upgrade head` as everything else. Without this the worker starts,
connects fine, and dies on its first housekeeping call with
`UndefinedFunction: procrastinate_prune_stalled_workers_v1(double precision)
does not exist` — a crash loop that looks like a connectivity problem but is a
missing-schema problem.

Procrastinate ships its schema as SQL rather than as ORM models, so we apply it
verbatim (`SchemaManager.get_schema()`) instead of restating it here. Restating
it would silently drift from whatever the pinned procrastinate version expects.

Runs as memory_owner: these objects are created in `public`, which memory_app
has no CREATE on by design (see ops/initdb/01-roles.sql).

Revision ID: 0002
Revises: 0001
"""
from alembic import op
from procrastinate.schema import SchemaManager

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# memory_app is the runtime role and must be able to enqueue, lock and finish
# jobs — but nothing more. It gets no CREATE on public and no ownership, so a
# compromised app role cannot redefine the queue's own functions.
GRANTS = """
GRANT USAGE ON SCHEMA public TO memory_app;
GRANT SELECT, INSERT, UPDATE, DELETE
  ON ALL TABLES IN SCHEMA public TO memory_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO memory_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO memory_app;
"""


def upgrade() -> None:
    # Raw cursor, not exec_driver_sql — see the note in 0001: psycopg only skips
    # placeholder parsing when params is None, and this script contains literal
    # '%' characters.
    with op.get_bind().connection.cursor() as cur:
        # PIN THE SCHEMA. Procrastinate's DDL uses unqualified names, so it lands
        # wherever search_path points. 01-SCHEMA.sql (migration 0001) ends with a
        # bare `SET search_path = mem, public`, and env.py runs every migration in
        # ONE transaction — so on a FRESH database that SET is still in effect
        # here and the entire queue schema is created in `mem`. The worker then
        # crash-loops on `procrastinate_prune_stalled_workers_v1 does not exist`
        # because it connects with the default search_path.
        #
        # It only reproduces on a fresh install: applied incrementally, 0002 runs
        # in its own session with a clean search_path and lands in `public`. That
        # divergence is why it survived several rebuilds unnoticed. Migration 0010
        # repairs databases already built the wrong way.
        cur.execute("SET LOCAL search_path = public")
        cur.execute(SchemaManager.get_schema())
        cur.execute(GRANTS)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(
            """
            DROP TABLE IF EXISTS procrastinate_jobs,
                                 procrastinate_events,
                                 procrastinate_periodic_defers,
                                 procrastinate_workers CASCADE;
            """
        )
