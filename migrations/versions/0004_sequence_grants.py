"""Grant sequence usage to memory_app, and fix the class of bug via defaults.

01-SCHEMA.sql ends with:

    GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA mem TO memory_app;

Tables only. Nothing grants the SEQUENCES, so every identity/serial column in
`mem` is unusable by the application role. That is not a corner case here:

  * memory_versions_id_seq  — mem.fn_memory_version() is an INSERT/UPDATE trigger
    on mem.memories, so WITHOUT this grant no memory can ever be written at all.
    The failure surfaces from inside the trigger as
    `permission denied for sequence memory_versions_id_seq`, which reads like a
    versioning problem rather than a grant problem.
  * audit_log_id_seq        — every audited operation.
  * ingestion_events_id_seq — the Phase 1 git ingestion path.

The GRANTs below fix the three sequences that exist today. The ALTER DEFAULT
PRIVILEGES statements fix the bug class: any table or sequence a later migration
creates as memory_owner in this schema is covered automatically, so the next
person to add a serial column does not rediscover this the hard way.

Default privileges are keyed to the CREATING role, which is why these are scoped
FOR ROLE memory_owner — migrations run as the owner (see the init service in
docker-compose.yml). They apply to future objects only; the explicit grants above
them cover what already exists.

DELETE stays excluded for memory_app, consistent with 01-SCHEMA.sql: deletion is
an explicit, audited admin operation.

Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


SQL = """
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA mem TO memory_app;

ALTER DEFAULT PRIVILEGES FOR ROLE memory_owner IN SCHEMA mem
  GRANT USAGE, SELECT ON SEQUENCES TO memory_app;
ALTER DEFAULT PRIVILEGES FOR ROLE memory_owner IN SCHEMA mem
  GRANT SELECT, INSERT, UPDATE ON TABLES TO memory_app;
ALTER DEFAULT PRIVILEGES FOR ROLE memory_owner IN SCHEMA mem
  GRANT SELECT ON TABLES TO memory_ro;
"""

DOWN = """
REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA mem FROM memory_app;

ALTER DEFAULT PRIVILEGES FOR ROLE memory_owner IN SCHEMA mem
  REVOKE USAGE, SELECT ON SEQUENCES FROM memory_app;
ALTER DEFAULT PRIVILEGES FOR ROLE memory_owner IN SCHEMA mem
  REVOKE SELECT, INSERT, UPDATE ON TABLES FROM memory_app;
ALTER DEFAULT PRIVILEGES FOR ROLE memory_owner IN SCHEMA mem
  REVOKE SELECT ON TABLES FROM memory_ro;
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(DOWN)
