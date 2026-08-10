"""Complete the RLS policies 01-SCHEMA.sql left as a TODO.

01-SCHEMA.sql enables and FORCEs row level security on every tenant-scoped table,
then defines policies for only two of them, with the comments:

    -- ... apply to every tenant-scoped table. A CI test asserts none are missed.
    -- Repeat the pattern for entities/relationships/mentions/conflicts/feedback.

Neither the repetition nor the CI test happened. The result is seven tables with
RLS enabled and FORCEd but no INSERT policy, six of them with no policies at all.
In Postgres that is a default-deny: memory_app cannot read them and cannot write
them. `memory_embeddings` is the one that bites immediately — a correctly scoped
insert fails with `new row violates row-level security policy`, so the platform
cannot store a single vector, which blocks the Phase 1 vertical slice and all of
Phase 3 retrieval.

Why it went unnoticed: the /readyz isolation self-test asserts that an UNSCOPED
read returns zero rows. A table nobody can write to satisfies that perfectly. The
check was never wrong — it only ever looked at reads.

Two policy shapes, both taken from the existing pattern rather than invented:

  * Tables carrying their own project_id (entities, relationships, conflicts,
    retrieval_events) follow memories_read/memories_write: read across the
    allowed projects, write only into the CURRENT project. A NULL project_id
    means tenant-wide; it is readable by anyone in the tenant but, exactly as
    with org-scope memories, NOT writable from the app path — promotion to
    tenant scope is a privileged operation.

  * Tables hanging off a memory (memory_embeddings, entity_mentions, feedback)
    inherit visibility through EXISTS on mem.memories, which is itself
    RLS-filtered. This is the emb_read pattern: one source of truth for which
    memories are visible, so these tables cannot drift from it.

DELETE policies are deliberately absent: 01-SCHEMA.sql grants memory_app only
SELECT/INSERT/UPDATE, and deletion stays an explicit audited admin operation.

retrieval_events gets no UPDATE policy on purpose — it is an append-only log, and
the app path should not be able to rewrite its own retrieval history.

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


# Read across every project the caller may see; write only into the current one.
PROJECT_SCOPED = """
CREATE POLICY {t}_read ON mem.{t} FOR SELECT
USING (
  tenant_id = mem.current_tenant()
  AND (project_id IS NULL OR project_id = ANY (mem.allowed_projects()))
);

CREATE POLICY {t}_write ON mem.{t} FOR INSERT
WITH CHECK (
  tenant_id = mem.current_tenant()
  AND project_id = nullif(current_setting('app.project_id', true), '')::uuid
);
"""

PROJECT_SCOPED_UPDATE = """
CREATE POLICY {t}_update ON mem.{t} FOR UPDATE
USING (
  tenant_id = mem.current_tenant()
  AND (project_id IS NULL OR project_id = ANY (mem.allowed_projects()))
)
WITH CHECK (tenant_id = mem.current_tenant());
"""

# Visibility inherited from the parent memory, which is itself RLS-filtered.
MEMORY_SCOPED = """
CREATE POLICY {t}_read ON mem.{t} FOR SELECT
USING (
  tenant_id = mem.current_tenant()
  AND EXISTS (SELECT 1 FROM mem.memories m WHERE m.id = {t}.{fk})
);

CREATE POLICY {t}_write ON mem.{t} FOR INSERT
WITH CHECK (
  tenant_id = mem.current_tenant()
  AND EXISTS (SELECT 1 FROM mem.memories m WHERE m.id = {t}.{fk})
);

CREATE POLICY {t}_update ON mem.{t} FOR UPDATE
USING (
  tenant_id = mem.current_tenant()
  AND EXISTS (SELECT 1 FROM mem.memories m WHERE m.id = {t}.{fk})
)
WITH CHECK (tenant_id = mem.current_tenant());
"""

# feedback.memory_id is nullable: feedback can target a retrieval pack rather than
# a specific memory, so the parent-memory check only applies when one is named.
FEEDBACK = """
CREATE POLICY feedback_read ON mem.feedback FOR SELECT
USING (
  tenant_id = mem.current_tenant()
  AND (memory_id IS NULL
       OR EXISTS (SELECT 1 FROM mem.memories m WHERE m.id = feedback.memory_id))
);

CREATE POLICY feedback_write ON mem.feedback FOR INSERT
WITH CHECK (
  tenant_id = mem.current_tenant()
  AND (memory_id IS NULL
       OR EXISTS (SELECT 1 FROM mem.memories m WHERE m.id = feedback.memory_id))
);
"""

PROJECT_TABLES = ["entities", "relationships", "conflicts", "retrieval_events"]
# Append-only: no UPDATE policy for retrieval_events.
PROJECT_UPDATABLE = ["entities", "relationships", "conflicts"]


def _sql() -> str:
    parts = [PROJECT_SCOPED.format(t=t) for t in PROJECT_TABLES]
    parts += [PROJECT_SCOPED_UPDATE.format(t=t) for t in PROJECT_UPDATABLE]
    parts.append(MEMORY_SCOPED.format(t="entity_mentions", fk="memory_id"))
    # memory_embeddings already has emb_read from 01-SCHEMA.sql; add only the
    # write paths, under the same predicate, so read and write cannot diverge.
    parts.append("""
CREATE POLICY emb_write ON mem.memory_embeddings FOR INSERT
WITH CHECK (
  tenant_id = mem.current_tenant()
  AND EXISTS (SELECT 1 FROM mem.memories m WHERE m.id = memory_embeddings.memory_id)
);

CREATE POLICY emb_update ON mem.memory_embeddings FOR UPDATE
USING (
  tenant_id = mem.current_tenant()
  AND EXISTS (SELECT 1 FROM mem.memories m WHERE m.id = memory_embeddings.memory_id)
)
WITH CHECK (tenant_id = mem.current_tenant());
""")
    parts.append(FEEDBACK)
    return "\n".join(parts)


DROP = """
DROP POLICY IF EXISTS {t}_read   ON mem.{t};
DROP POLICY IF EXISTS {t}_write  ON mem.{t};
DROP POLICY IF EXISTS {t}_update ON mem.{t};
"""


def upgrade() -> None:
    # Raw cursor rather than exec_driver_sql — see the note in 0001.
    with op.get_bind().connection.cursor() as cur:
        cur.execute(_sql())


def downgrade() -> None:
    stmts = [DROP.format(t=t) for t in PROJECT_TABLES + ["entity_mentions", "feedback"]]
    stmts.append(
        "DROP POLICY IF EXISTS emb_write ON mem.memory_embeddings;"
        "DROP POLICY IF EXISTS emb_update ON mem.memory_embeddings;"
    )
    with op.get_bind().connection.cursor() as cur:
        cur.execute("\n".join(stmts))
