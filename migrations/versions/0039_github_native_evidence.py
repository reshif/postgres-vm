"""GitHub-native delivery and evidence ledger.

The legacy `.memory` checkout is a temporary input, not the durable authority
for a multi-host project. This migration establishes the data contract for the
replacement: signed provider deliveries, immutable evidence artifacts, and
reviewed assertions linked to their evidence. PostgreSQL is a query projection;
source code and reviewed assertion files live in Git at exact revisions.

No table stores raw webhook payloads. PR bodies, workflow logs, and other
unreviewed free text are not safe project knowledge and must never become
retrieval candidates merely because GitHub delivered an event.

Revision ID: 0039
Revises: 0037
"""
from alembic import op

revision = "0039"
down_revision = "0037"
branch_labels = None
depends_on = None


SQL = """
ALTER TABLE mem.projects
  ADD COLUMN IF NOT EXISTS source_provider text NOT NULL DEFAULT 'legacy',
  ADD COLUMN IF NOT EXISTS evidence_repo_url text,
  ADD COLUMN IF NOT EXISTS github_installation_id bigint,
  ADD COLUMN IF NOT EXISTS git_default_branch text;

ALTER TABLE mem.projects
  ADD CONSTRAINT projects_source_provider_check
  CHECK (source_provider IN ('legacy', 'github'));

CREATE TABLE mem.github_deliveries (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            uuid NOT NULL REFERENCES mem.organizations(id) ON DELETE CASCADE,
    project_id           uuid NOT NULL REFERENCES mem.projects(id) ON DELETE CASCADE,
    provider             text NOT NULL DEFAULT 'github' CHECK (provider = 'github'),
    delivery_id          text NOT NULL,
    event_name           text NOT NULL CHECK (event_name IN
                           ('check_run', 'deployment_status', 'pull_request', 'push', 'workflow_run')),
    repository_url       text NOT NULL,
    repository_full_name text NOT NULL,
    revision             text,
    ref                  text,
    occurred_at          timestamptz NOT NULL,
    payload_sha256       text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    metadata             jsonb NOT NULL DEFAULT '{}'::jsonb,
    status               text NOT NULL DEFAULT 'received'
                         CHECK (status IN ('received', 'queued', 'processed', 'ignored', 'failed')),
    error                text,
    received_at          timestamptz NOT NULL DEFAULT now(),
    processed_at         timestamptz,
    UNIQUE (provider, delivery_id)
);
CREATE INDEX idx_github_deliveries_project_status
  ON mem.github_deliveries (tenant_id, project_id, status, received_at DESC);

CREATE TABLE mem.evidence_artifacts (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES mem.organizations(id) ON DELETE CASCADE,
    project_id        uuid NOT NULL REFERENCES mem.projects(id) ON DELETE CASCADE,
    provider          text NOT NULL CHECK (provider IN ('github', 'agent', 'external')),
    kind              text NOT NULL CHECK (kind IN
                      ('git_event', 'git_commit', 'git_blob', 'pull_request', 'ci_run',
                       'deployment', 'agent_episode', 'external_document')),
    external_id       text NOT NULL,
    source_repository text NOT NULL,
    source_revision   text,
    source_ref        text,
    location          text,
    content_sha256    text CHECK (content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'),
    byte_size         integer CHECK (byte_size IS NULL OR byte_size >= 0),
    observed_at       timestamptz NOT NULL,
    recorded_at       timestamptz NOT NULL DEFAULT now(),
    metadata          jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (project_id, provider, kind, external_id)
);
CREATE INDEX idx_evidence_artifacts_project_revision
  ON mem.evidence_artifacts (tenant_id, project_id, source_revision, observed_at DESC);

CREATE TABLE mem.evidence_assertions (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES mem.organizations(id) ON DELETE CASCADE,
    project_id        uuid NOT NULL REFERENCES mem.projects(id) ON DELETE CASCADE,
    assertion_key     text NOT NULL,
    subject           text NOT NULL,
    predicate         text NOT NULL,
    object_value      text NOT NULL,
    attributes        jsonb NOT NULL DEFAULT '{}'::jsonb,
    state             text NOT NULL DEFAULT 'proposed'
                      CHECK (state IN ('proposed', 'accepted', 'contested', 'retracted', 'superseded')),
    confidence        numeric(4,3) NOT NULL DEFAULT 1.000
                      CHECK (confidence >= 0 AND confidence <= 1),
    source_repository text NOT NULL,
    source_path       text NOT NULL,
    source_revision   text NOT NULL,
    valid_at          tstzrange NOT NULL DEFAULT tstzrange(now(), NULL, '[)'),
    recorded_at       timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    accepted_at       timestamptz,
    accepted_by       uuid REFERENCES mem.principals(id),
    superseded_by     uuid REFERENCES mem.evidence_assertions(id),
    UNIQUE (project_id, assertion_key, source_revision)
);
CREATE INDEX idx_evidence_assertions_retrieval
  ON mem.evidence_assertions (tenant_id, project_id, state, recorded_at DESC);

CREATE TABLE mem.assertion_evidence (
    assertion_id      uuid NOT NULL REFERENCES mem.evidence_assertions(id) ON DELETE CASCADE,
    artifact_id       uuid NOT NULL REFERENCES mem.evidence_artifacts(id) ON DELETE RESTRICT,
    tenant_id         uuid NOT NULL REFERENCES mem.organizations(id) ON DELETE CASCADE,
    project_id        uuid NOT NULL REFERENCES mem.projects(id) ON DELETE CASCADE,
    role              text NOT NULL DEFAULT 'supports'
                      CHECK (role IN ('supports', 'contradicts', 'derived_from')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (assertion_id, artifact_id, role)
);
CREATE INDEX idx_assertion_evidence_scope
  ON mem.assertion_evidence (tenant_id, project_id, assertion_id);

ALTER TABLE mem.github_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE mem.github_deliveries FORCE ROW LEVEL SECURITY;
ALTER TABLE mem.evidence_artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE mem.evidence_artifacts FORCE ROW LEVEL SECURITY;
ALTER TABLE mem.evidence_assertions ENABLE ROW LEVEL SECURITY;
ALTER TABLE mem.evidence_assertions FORCE ROW LEVEL SECURITY;
ALTER TABLE mem.assertion_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE mem.assertion_evidence FORCE ROW LEVEL SECURITY;

CREATE POLICY github_deliveries_read ON mem.github_deliveries FOR SELECT
USING (tenant_id = mem.current_tenant() AND project_id = ANY (mem.allowed_projects()));
CREATE POLICY github_deliveries_write ON mem.github_deliveries FOR INSERT
WITH CHECK (tenant_id = mem.current_tenant()
            AND project_id = nullif(current_setting('app.project_id', true), '')::uuid);
CREATE POLICY github_deliveries_update ON mem.github_deliveries FOR UPDATE
USING (tenant_id = mem.current_tenant() AND project_id = ANY (mem.allowed_projects()))
WITH CHECK (tenant_id = mem.current_tenant()
            AND project_id = nullif(current_setting('app.project_id', true), '')::uuid);

CREATE POLICY evidence_artifacts_read ON mem.evidence_artifacts FOR SELECT
USING (tenant_id = mem.current_tenant() AND project_id = ANY (mem.allowed_projects()));
CREATE POLICY evidence_artifacts_write ON mem.evidence_artifacts FOR INSERT
WITH CHECK (tenant_id = mem.current_tenant()
            AND project_id = nullif(current_setting('app.project_id', true), '')::uuid);

CREATE POLICY evidence_assertions_read ON mem.evidence_assertions FOR SELECT
USING (tenant_id = mem.current_tenant() AND project_id = ANY (mem.allowed_projects()));
CREATE POLICY evidence_assertions_write ON mem.evidence_assertions FOR INSERT
WITH CHECK (tenant_id = mem.current_tenant()
            AND project_id = nullif(current_setting('app.project_id', true), '')::uuid);
CREATE POLICY evidence_assertions_update ON mem.evidence_assertions FOR UPDATE
USING (tenant_id = mem.current_tenant() AND project_id = ANY (mem.allowed_projects()))
WITH CHECK (tenant_id = mem.current_tenant()
            AND project_id = nullif(current_setting('app.project_id', true), '')::uuid);

CREATE POLICY assertion_evidence_read ON mem.assertion_evidence FOR SELECT
USING (tenant_id = mem.current_tenant() AND project_id = ANY (mem.allowed_projects()));
CREATE POLICY assertion_evidence_write ON mem.assertion_evidence FOR INSERT
WITH CHECK (tenant_id = mem.current_tenant()
            AND project_id = nullif(current_setting('app.project_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE ON mem.github_deliveries TO memory_app;
GRANT SELECT, INSERT ON mem.evidence_artifacts TO memory_app;
GRANT SELECT, INSERT, UPDATE ON mem.evidence_assertions TO memory_app;
GRANT SELECT, INSERT ON mem.assertion_evidence TO memory_app;
"""


DOWN = """
DROP TABLE IF EXISTS mem.assertion_evidence;
DROP TABLE IF EXISTS mem.evidence_assertions;
DROP TABLE IF EXISTS mem.evidence_artifacts;
DROP TABLE IF EXISTS mem.github_deliveries;
ALTER TABLE mem.projects DROP CONSTRAINT IF EXISTS projects_source_provider_check;
ALTER TABLE mem.projects DROP COLUMN IF EXISTS git_default_branch;
ALTER TABLE mem.projects DROP COLUMN IF EXISTS github_installation_id;
ALTER TABLE mem.projects DROP COLUMN IF EXISTS evidence_repo_url;
ALTER TABLE mem.projects DROP COLUMN IF EXISTS source_provider;
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(DOWN)
