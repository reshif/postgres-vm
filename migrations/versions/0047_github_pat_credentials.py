"""Encrypted, project-scoped GitHub fine-grained PAT credentials.

The GitHub App remains the preferred integration and owns webhook delivery.
This table provides a deliberately narrow alternative for a project operator
who needs a fine-grained PAT for the same immutable Git reads.  The secret is
encrypted by the application before it reaches PostgreSQL; none of the API
read models ever return ``token_ciphertext``.

Revision ID: 0047
Revises: 0045
"""
from alembic import op

revision = "0047"
down_revision = "0045"
branch_labels = None
depends_on = None


SQL = """
CREATE TABLE mem.github_pat_credentials (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL REFERENCES mem.organizations(id) ON DELETE CASCADE,
    project_id       uuid NOT NULL REFERENCES mem.projects(id) ON DELETE CASCADE,
    provider         text NOT NULL DEFAULT 'github_pat'
                     CHECK (provider = 'github_pat'),
    -- A Fernet ciphertext, never the PAT itself.  Decryption requires the
    -- deployment-owned MEMORY_GITHUB_PAT_ENCRYPTION_KEY.
    token_ciphertext bytea NOT NULL,
    token_hint       text NOT NULL CHECK (token_hint ~ '^\\*{4}[A-Za-z0-9_-]{4}$'),
    token_fingerprint text NOT NULL CHECK (token_fingerprint ~ '^[0-9a-f]{64}$'),
    github_login     text,
    scopes           text[] NOT NULL DEFAULT ARRAY[]::text[],
    validated_at     timestamptz NOT NULL DEFAULT now(),
    last_used_at     timestamptz,
    last_error       text,
    created_by       uuid REFERENCES mem.principals(id),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, provider)
);
CREATE INDEX idx_github_pat_credentials_scope
  ON mem.github_pat_credentials (tenant_id, project_id);

ALTER TABLE mem.github_pat_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE mem.github_pat_credentials FORCE ROW LEVEL SECURITY;

CREATE POLICY github_pat_credentials_select ON mem.github_pat_credentials FOR SELECT
  USING (tenant_id = mem.current_tenant()
         AND project_id = ANY(mem.allowed_projects()));
CREATE POLICY github_pat_credentials_insert ON mem.github_pat_credentials FOR INSERT
  WITH CHECK (tenant_id = mem.current_tenant()
              AND project_id = nullif(current_setting('app.project_id', true), '')::uuid);
CREATE POLICY github_pat_credentials_update ON mem.github_pat_credentials FOR UPDATE
  USING (tenant_id = mem.current_tenant()
         AND project_id = nullif(current_setting('app.project_id', true), '')::uuid)
  WITH CHECK (tenant_id = mem.current_tenant()
              AND project_id = nullif(current_setting('app.project_id', true), '')::uuid);
CREATE POLICY github_pat_credentials_delete ON mem.github_pat_credentials FOR DELETE
  USING (tenant_id = mem.current_tenant()
         AND project_id = nullif(current_setting('app.project_id', true), '')::uuid);

GRANT SELECT, INSERT, UPDATE, DELETE ON mem.github_pat_credentials TO memory_app;
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS mem.github_pat_credentials;")
