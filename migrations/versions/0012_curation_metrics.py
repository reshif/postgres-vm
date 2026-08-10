"""Curation metrics — the substrate for ADR-0015's kill switch.

05-BUILD-PLAN.md Phase 5: "inbox depth per project, weekly review minutes, alert
at depth 100, automatic disable of LLM extraction at depth 200 sustained for two
weeks. The kill switch is tested by simulation before extraction is enabled."

"Sustained for two weeks" is not answerable from the present state of the inbox.
A queue at depth 250 today might have been at 12 yesterday — that is a burst, not
an abandoned queue, and disabling extraction for it would be wrong. So depth has
to be SAMPLED OVER TIME, and that needs a table.

DAILY GRAIN, deliberately. Sampling more often would let one quiet hour reset a
two-week clock; sampling less often cannot distinguish a burst from neglect. One
row per project per day, upserted, so the scheduler running every 60 seconds
costs one write per project per day rather than 1440.

ACCEPTANCE COUNTERS live here too. Phase 5 wants extraction acceptance rate in a
30-85% band. Below 30% the extractor is generating noise a human must clear;
above 85% the reviewer has almost certainly stopped reading and is clicking
accept. Both ends are failures, and both are only visible as a ratio over time.

Revision ID: 0012
Revises: 0011
"""
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


SQL = """
CREATE TABLE IF NOT EXISTS mem.curation_metrics (
    id            bigserial PRIMARY KEY,
    tenant_id     uuid NOT NULL REFERENCES mem.organizations(id) ON DELETE CASCADE,
    project_id    uuid NOT NULL REFERENCES mem.projects(id) ON DELETE CASCADE,
    observed_on   date NOT NULL DEFAULT current_date,

    -- Queue state at sample time.
    inbox_depth   integer NOT NULL DEFAULT 0,
    oldest_days   integer NOT NULL DEFAULT 0,

    -- Review throughput for that day, from the audit log.
    promoted      integer NOT NULL DEFAULT 0,
    rejected      integer NOT NULL DEFAULT 0,

    -- Extraction proposals written that day (the denominator of the
    -- acceptance-rate band).
    extracted     integer NOT NULL DEFAULT 0,

    sampled_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT curation_metrics_uniq UNIQUE (tenant_id, project_id, observed_on)
);

CREATE INDEX IF NOT EXISTS idx_curation_recent
  ON mem.curation_metrics (tenant_id, project_id, observed_on DESC);

ALTER TABLE mem.curation_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE mem.curation_metrics FORCE ROW LEVEL SECURITY;

-- Read within scope. The kill switch has to be readable by the application role
-- that the switch governs, or the switch cannot be consulted on the write path.
DROP POLICY IF EXISTS curation_metrics_select ON mem.curation_metrics;
CREATE POLICY curation_metrics_select ON mem.curation_metrics
    FOR SELECT USING (tenant_id = mem.current_tenant());

DROP POLICY IF EXISTS curation_metrics_insert ON mem.curation_metrics;
CREATE POLICY curation_metrics_insert ON mem.curation_metrics
    FOR INSERT WITH CHECK (tenant_id = mem.current_tenant());

-- UPDATE is the upsert path for the current day's sample only. Rewriting
-- yesterday's depth is how a two-week window quietly becomes a one-day window,
-- so the policy pins it to today.
DROP POLICY IF EXISTS curation_metrics_update ON mem.curation_metrics;
CREATE POLICY curation_metrics_update ON mem.curation_metrics
    FOR UPDATE USING (tenant_id = mem.current_tenant() AND observed_on = current_date)
             WITH CHECK (tenant_id = mem.current_tenant() AND observed_on = current_date);

GRANT SELECT, INSERT, UPDATE ON mem.curation_metrics TO memory_app;
GRANT USAGE, SELECT ON SEQUENCE mem.curation_metrics_id_seq TO memory_app;
"""

DOWN = """
DROP TABLE IF EXISTS mem.curation_metrics;
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(DOWN)
