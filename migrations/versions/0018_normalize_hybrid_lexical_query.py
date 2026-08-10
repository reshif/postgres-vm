"""Normalize an intermediate nested lexical-query trim.

The first local application of 0017 replaced the raw expression inside an
already-trimmed function body. ``btrim`` is idempotent, so retrieval remained
correct, but migrations must converge on one inspectable SQL definition.

Revision ID: 0018
Revises: 0017
"""
from __future__ import annotations

from alembic import op


revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_SIGNATURE = (
    "mem.search_hybrid(halfvec,text,mem.memory_type[],mem.trust_tier,"
    "timestamptz,uuid[],integer,integer,mem.memory_status[])"
)
_SINGLE = "btrim(regexp_replace(lower(p_query_text), '[^[:alnum:]_]+', ' | ', 'g'), ' |')"
_NESTED = "btrim(" + _SINGLE + ", ' |')"


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_functiondef(%s::regprocedure)", (_SIGNATURE,))
        definition = cursor.fetchone()[0]
        if _NESTED not in definition:
            if _SINGLE in definition:
                return
            raise RuntimeError("hybrid lexical function body was not found")
        cursor.execute(definition.replace(_NESTED, _SINGLE))


def downgrade() -> None:
    raise RuntimeError("Downgrade requires restoring the 0017 function body")
