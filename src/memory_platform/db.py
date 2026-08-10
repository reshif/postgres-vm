"""Database access, and the only sanctioned way to open a scoped transaction.

The scope context is set with set_config(..., true) — transaction-local. Under
transaction-mode pooling a bare SET would persist on the shared connection and the
next request would inherit another tenant's context. That is the exact leak the
whole isolation model exists to prevent, so the unsafe path is not exposed here.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from .config import settings

_engine: Engine | None = None
_engine_direct: Engine | None = None


def _make(url: str) -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
        # psycopg3 named prepared statements do not survive transaction pooling.
        # PgBouncer's max_prepared_statements handles the server side; this is the
        # client side of the same fix.
        connect_args={"prepare_threshold": None}
        if settings().db_prepare_threshold == 0
        else {},
    )


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = _make(settings().database_url)
    return _engine


def engine_direct() -> Engine:
    global _engine_direct
    if _engine_direct is None:
        _engine_direct = _make(settings().database_url_direct)
    return _engine_direct


@contextmanager
def scoped(
    tenant_id: UUID,
    principal_id: UUID,
    project_id: UUID | None,
    project_ids: list[UUID] | None = None,
) -> Iterator[Connection]:
    """Open a transaction with RLS scope context established.

    There is deliberately no unscoped equivalent. If you need to read without a
    scope, you are writing an admin tool and should say so explicitly.
    """
    projects = project_ids if project_ids is not None else ([project_id] if project_id else [])
    with engine().begin() as conn:
        conn.execute(
            text("SELECT mem.fn_set_scope(:t, :pr, :p, CAST(:ps AS uuid[]))"),
            {
                "t": str(tenant_id),
                "pr": str(principal_id),
                "p": str(project_id) if project_id else None,
                "ps": "{" + ",".join(str(p) for p in projects) + "}",
            },
        )
        yield conn


def ping(direct: bool = False) -> bool:
    eng = engine_direct() if direct else engine()
    with eng.connect() as conn:
        return conn.execute(text("SELECT 1")).scalar_one() == 1


def isolation_selftest() -> dict:
    """With no scope context set, RLS must return zero rows.

    This is the single most important assertion in the system. It runs on /readyz
    so a misconfigured deployment is loud rather than quiet.
    """
    with engine().connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM mem.memories")).scalar_one()
    return {"unscoped_rows_visible": int(n), "pass": int(n) == 0}
