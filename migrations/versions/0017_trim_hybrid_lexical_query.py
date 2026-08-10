"""Trim punctuation separators in the 0016 relaxed lexical tsquery.

Revision ID: 0017
Revises: 0016
"""
from __future__ import annotations

from alembic import op


revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_SIGNATURE = (
    "mem.search_hybrid(halfvec,text,mem.memory_type[],mem.trust_tier,"
    "timestamptz,uuid[],integer,integer,mem.memory_status[])"
)
_OLD = "regexp_replace(lower(p_query_text), '[^[:alnum:]_]+', ' | ', 'g')"
_NEW = "btrim(regexp_replace(lower(p_query_text), '[^[:alnum:]_]+', ' | ', 'g'), ' |')"


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_functiondef(%s::regprocedure)", (_SIGNATURE,))
        definition = cursor.fetchone()[0]
        # 0016 in a fresh checkout already carries the trimmed expression. The
        # first local application of this repair began from its earlier raw
        # version, so retain that upgrade path without nesting btrim on fresh
        # installs.
        if _NEW in definition:
            return
        if _OLD not in definition:
            raise RuntimeError("0016 hybrid lexical function body was not found")
        cursor.execute(definition.replace(_OLD, _NEW))


def downgrade() -> None:
    raise RuntimeError("Downgrade requires restoring the 0016 function body")
