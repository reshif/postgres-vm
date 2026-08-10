"""Initial schema — executes 01-SCHEMA.sql verbatim.

The schema lives in a reviewed .sql file rather than in Python DDL on purpose:
the RLS policies, temporal constraints and the hybrid-search function are the
security and correctness surface of this system, and they should be readable and
diffable as SQL, not reconstructed from an ORM.

Revision ID: 0001
Revises:
"""
from pathlib import Path
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# /app/sql/01-SCHEMA.sql in the image; ../../01-SCHEMA.sql when run from a checkout.
CANDIDATES = [
    Path("/app/sql/01-SCHEMA.sql"),
    Path(__file__).resolve().parents[2] / "01-SCHEMA.sql",
]


def _schema_sql() -> str:
    for p in CANDIDATES:
        if p.exists():
            return p.read_text(encoding="utf-8")
    raise SystemExit(f"01-SCHEMA.sql not found; looked in {CANDIDATES}")


def upgrade() -> None:
    sql = _schema_sql()
    # Go through the raw psycopg cursor, NOT exec_driver_sql. psycopg3 only skips
    # placeholder parsing and use the simple query protocol — which is what lets a
    # multi-statement script run as one batch — when params is None. SQLAlchemy's
    # exec_driver_sql passes an empty tuple instead, so psycopg scans the script
    # for placeholders and dies on the first literal '%' with
    # `incomplete placeholder: '%'`. Doubling up every '%' in reviewed SQL to
    # satisfy a driver we are not asking to bind anything is the wrong trade.
    with op.get_bind().connection.cursor() as cur:
        cur.execute(sql)


def downgrade() -> None:
    op.get_bind().exec_driver_sql("DROP SCHEMA IF EXISTS mem CASCADE;")
