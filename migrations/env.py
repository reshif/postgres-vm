"""Alembic environment.

Connects DIRECT to Postgres, never through PgBouncer: Alembic takes session-level
advisory locks and runs DDL, neither of which is safe under transaction pooling.
"""
import os
from alembic import context
from sqlalchemy import create_engine, pool

config = context.config


def _url() -> str:
    x = context.get_x_argument(as_dictionary=True)
    url = x.get("url") or os.environ.get("MEMORY_DATABASE_URL_DIRECT")
    if not url:
        raise SystemExit(
            "no database URL. Pass -x url=... or set MEMORY_DATABASE_URL_DIRECT"
        )
    if "@pgbouncer" in url:
        raise SystemExit(
            "refusing to migrate through PgBouncer — use the direct URL "
            "(see the connection topology note in docker-compose.yml)"
        )
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
