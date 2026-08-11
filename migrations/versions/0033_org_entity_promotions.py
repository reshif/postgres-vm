"""Proposals to promote a project entity to organisation scope.

ADR-0012 makes cross-project generalisation opt-in, deferred, and human-approved:

    generalise -> strip project-specific identifiers, URLs, hostnames and secrets
    -> propose -> human approve -> shared knowledge. Raw memories never cross
    project boundaries.

`mem.entities.project_id` has always been nullable and the read policy has always
admitted `project_id IS NULL`, so an organisation-scoped entity was already
retrievable across a tenant's projects. Nothing could create one, which is the
correct state to have been in — the crossing needs a review step, and this table
is it.

The proposal is a row rather than a direct write because "propose -> human
approve" is the whole control. A promotion that happened as a side effect of
extraction would move knowledge across a project boundary with no reviewer and no
record, which is the failure ADR-0012 is written to prevent.

Revision ID: 0033
Revises: 0031
"""
from alembic import op

revision = "0033"
down_revision = "0031"
branch_labels = None
depends_on = None


SQL = """
CREATE TABLE IF NOT EXISTS mem.proposed_org_entities (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES mem.organizations(id) ON DELETE CASCADE,
    -- The project the entity is being promoted OUT OF. Retained after the
    -- decision so "who proposed sharing this" stays answerable.
    project_id      uuid NOT NULL REFERENCES mem.projects(id) ON DELETE CASCADE,
    entity_id       uuid NOT NULL REFERENCES mem.entities(id) ON DELETE CASCADE,
    kind            text NOT NULL,
    canonical_name  text NOT NULL,
    -- The generalised form actually proposed for sharing, which may differ from
    -- the project-local one. Stored so the reviewer approves the exact text that
    -- will be shared rather than approving an intention.
    proposed_name   text NOT NULL,
    attributes      jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Why the screen let this through, or what a reviewer should look at. An
    -- approval with no visible reasoning is a rubber stamp.
    rationale       jsonb NOT NULL DEFAULT '{}'::jsonb,
    decision        text CHECK (decision IN ('accepted', 'rejected')),
    decided_at      timestamptz,
    decided_by      uuid REFERENCES mem.principals(id),
    reason          text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT proposed_org_entity_unique UNIQUE (tenant_id, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_proposed_org_entities_pending
  ON mem.proposed_org_entities (tenant_id, project_id, created_at DESC)
  WHERE decision IS NULL;

ALTER TABLE mem.proposed_org_entities ENABLE ROW LEVEL SECURITY;
ALTER TABLE mem.proposed_org_entities FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS proposed_org_entities_select ON mem.proposed_org_entities;
CREATE POLICY proposed_org_entities_select ON mem.proposed_org_entities FOR SELECT
  USING (tenant_id = mem.current_tenant()
         AND project_id = ANY(mem.allowed_projects()));

DROP POLICY IF EXISTS proposed_org_entities_insert ON mem.proposed_org_entities;
CREATE POLICY proposed_org_entities_insert ON mem.proposed_org_entities FOR INSERT
  WITH CHECK (tenant_id = mem.current_tenant()
              AND project_id = nullif(current_setting('app.project_id', true), '')::uuid);

DROP POLICY IF EXISTS proposed_org_entities_update ON mem.proposed_org_entities;
CREATE POLICY proposed_org_entities_update ON mem.proposed_org_entities FOR UPDATE
  USING (tenant_id = mem.current_tenant()
         AND project_id = nullif(current_setting('app.project_id', true), '')::uuid)
  WITH CHECK (tenant_id = mem.current_tenant()
              AND project_id = nullif(current_setting('app.project_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON mem.proposed_org_entities TO memory_app;

-- Promotion writes an entity row with project_id IS NULL. The existing INSERT
-- policy on mem.entities requires project_id to equal the session's project, so
-- without this an approved promotion fails on the policy rather than on any
-- decision anyone made. Restricted to NULL specifically: this permits sharing
-- within the tenant, never writing into another project.
DROP POLICY IF EXISTS entities_insert_org ON mem.entities;
CREATE POLICY entities_insert_org ON mem.entities FOR INSERT
  WITH CHECK (tenant_id = mem.current_tenant() AND project_id IS NULL);
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute("DROP POLICY IF EXISTS entities_insert_org ON mem.entities;")
        cur.execute("DROP TABLE IF EXISTS mem.proposed_org_entities;")
