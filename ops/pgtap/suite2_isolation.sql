-- Suite 2 at the DATABASE layer (04-EVALUATION.md §3).
--
-- "Implemented twice, deliberately: pgTAP at the database layer and API-level
--  through the real gateway. Policy regressions are silent — no error, just
--  wrong rows — so automated cross-tenant tests are the only reliable guard."
--
-- Only the API half existed. This is the other one, and it is not a duplicate:
-- it asserts things the API-level suite structurally cannot.
--
--   * It runs as memory_app with scope set by hand, so it tests the POLICY
--     rather than the application's use of the policy. A refactor that stops
--     calling db.scoped() would still pass the Python suite and fail here.
--   * It can issue grants, because it runs where grants are issued. The
--     application role deliberately cannot — tests/test_isolation.py asserts
--     that it cannot — so the grant ladder (ceiling, expiry) is only testable
--     from this side.
--   * It asserts RLS is ENABLED AND FORCED on every table in mem, including
--     tables added after this file was written.
--
-- Run:  docker compose exec -T postgres psql -U memory_owner -d memory \
--         -f /pgtap/suite2_isolation.sql

\set ON_ERROR_STOP on
CREATE EXTENSION IF NOT EXISTS pgtap;

BEGIN;

-- 3 structural + 9 behavioural.
SELECT plan(12);

-- ---------------------------------------------------------------- structural
-- Enumerated, not listed: a table added next month is covered the moment it
-- exists, which is the only way an assertion like this stays true.
--
-- The exemptions mirror tests/test_rls_coverage.py exactly, and are deliberate
-- rather than oversights. `projects` and `principals` are resolved BEFORE a
-- scope exists — binding a project and authenticating a subject are what
-- produce the scope, so they cannot be gated by it. `organizations`,
-- `embedding_models` and `ranking_profiles` are catalogues: the tenant list,
-- the vector-space registry and the ranking weights. None carries memory
-- content.
--
-- Two suites asserting the same exemption list is the point. If someone adds a
-- table and quietly exempts it in Python, this file still fails.
SELECT is(
    (SELECT count(*)::int FROM pg_class c
       JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'mem' AND c.relkind = 'r'
        AND c.relname NOT IN ('projects', 'principals', 'organizations',
                              'embedding_models', 'ranking_profiles')
        AND NOT c.relrowsecurity),
    0,
    'every content-bearing table in mem has RLS enabled'
);

-- FORCE matters separately: without it the table OWNER bypasses its own
-- policies, and the owner is who migrations and admin tooling connect as.
SELECT is(
    (SELECT count(*)::int FROM pg_class c
       JOIN pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'mem' AND c.relkind = 'r'
        AND c.relname NOT IN ('projects', 'principals', 'organizations',
                              'embedding_models', 'ranking_profiles')
        AND NOT c.relforcerowsecurity),
    0,
    'every content-bearing table in mem has RLS FORCED'
);

SELECT has_function('mem', 'sensitivity_allowed', ARRAY['mem.sensitivity'],
    'the sensitivity gate exists as a function the policy can call');

-- --------------------------------------------------------------- behavioural
-- Two tenants, one restricted memory, one principal. Everything below runs as
-- memory_app with scope set explicitly, which is what the application does.
CREATE TEMP TABLE t AS
SELECT '00000000-0000-4000-8000-00000000ff01'::uuid AS tenant_a,
       '00000000-0000-4000-8000-00000000ff02'::uuid AS proj_a,
       '00000000-0000-4000-8000-00000000ff03'::uuid AS prin_a,
       '00000000-0000-4000-8000-00000000ff11'::uuid AS tenant_b,
       '00000000-0000-4000-8000-00000000ff12'::uuid AS proj_b,
       '00000000-0000-4000-8000-00000000ff13'::uuid AS prin_b;

-- The fixture table is created as the owner but read after SET ROLE, so it
-- needs an explicit grant. Note this is a TEMP table holding only UUIDs — the
-- suite still reads every real table as memory_app, which is the whole point.
GRANT SELECT ON t TO memory_app;

INSERT INTO mem.organizations (id, slug, name)
SELECT tenant_a, 'pgtap-a', 'pgtap A' FROM t
UNION ALL SELECT tenant_b, 'pgtap-b', 'pgtap B' FROM t
ON CONFLICT DO NOTHING;

INSERT INTO mem.projects (id, tenant_id, slug, name)
SELECT proj_a, tenant_a, 'pgtap-pa', 'pgtap PA' FROM t
UNION ALL SELECT proj_b, tenant_b, 'pgtap-pb', 'pgtap PB' FROM t
ON CONFLICT DO NOTHING;

INSERT INTO mem.principals (id, tenant_id, actor, external_id, display_name)
SELECT prin_a, tenant_a, 'agent'::mem.actor_type, 'pgtap-a', 'pgtap' FROM t
UNION ALL SELECT prin_b, tenant_b, 'agent'::mem.actor_type, 'pgtap-b', 'pgtap' FROM t
ON CONFLICT DO NOTHING;

INSERT INTO mem.memories
    (id, tenant_id, memory_key, type, title, content, digest, scope_kind,
     project_id, tier, source_type, content_hash, sensitivity)
SELECT '00000000-0000-4000-8000-00000000fe01', tenant_a, 'pgtap-secret',
       'decision', 'Restricted decision', 'Contents of a restricted decision.',
       'Restricted.', 'project', proj_a, 'authoritative', 'git',
       'pgtap-secret-hash', 'restricted'
  FROM t
ON CONFLICT DO NOTHING;

INSERT INTO mem.memories
    (id, tenant_id, memory_key, type, title, content, digest, scope_kind,
     project_id, tier, source_type, content_hash, sensitivity)
SELECT '00000000-0000-4000-8000-00000000fe02', tenant_a, 'pgtap-open',
       'decision', 'Ordinary decision', 'Contents of an ordinary decision.',
       'Ordinary.', 'project', proj_a, 'authoritative', 'git',
       'pgtap-open-hash', 'internal'
  FROM t
ON CONFLICT DO NOTHING;

-- Everything from here runs WITHOUT superuser, or none of it means anything.
SET LOCAL ROLE memory_app;

SELECT mem.fn_set_scope(tenant_a, prin_a, proj_a, ARRAY[proj_a]) FROM t;

SELECT is(
    (SELECT count(*)::int FROM mem.memories WHERE memory_key = 'pgtap-open'),
    1, 'an internal memory is visible inside its own scope');

SELECT is(
    (SELECT count(*)::int FROM mem.memories WHERE memory_key = 'pgtap-secret'),
    0, 'a restricted memory is NOT visible without a grant');

-- The ceiling is a ceiling, not a door.
RESET ROLE;
INSERT INTO mem.scope_grants
    (tenant_id, from_kind, from_id, to_kind, to_id, permission, reason,
     granted_by, max_sensitivity)
SELECT tenant_a, 'project', proj_a, 'user', prin_a, 'read', 'pgtap',
       prin_a, 'confidential' FROM t;
SET LOCAL ROLE memory_app;
SELECT mem.fn_set_scope(tenant_a, prin_a, proj_a, ARRAY[proj_a]) FROM t;

SELECT is(
    (SELECT count(*)::int FROM mem.memories WHERE memory_key = 'pgtap-secret'),
    0, 'a grant that stops at confidential does not open restricted');

RESET ROLE;
UPDATE mem.scope_grants SET max_sensitivity = 'restricted'
 WHERE to_id = (SELECT prin_a FROM t);
SET LOCAL ROLE memory_app;
SELECT mem.fn_set_scope(tenant_a, prin_a, proj_a, ARRAY[proj_a]) FROM t;

SELECT is(
    (SELECT count(*)::int FROM mem.memories WHERE memory_key = 'pgtap-secret'),
    1, 'a grant reaching restricted does return it');

-- Expiry is evaluated per query, so an expired grant is indistinguishable from
-- no grant rather than from one that used to work.
RESET ROLE;
UPDATE mem.scope_grants SET expires_at = now() - interval '1 day'
 WHERE to_id = (SELECT prin_a FROM t);
SET LOCAL ROLE memory_app;
SELECT mem.fn_set_scope(tenant_a, prin_a, proj_a, ARRAY[proj_a]) FROM t;

SELECT is(
    (SELECT count(*)::int FROM mem.memories WHERE memory_key = 'pgtap-secret'),
    0, 'an EXPIRED grant behaves exactly like no grant');

-- A grant belongs to the principal it names.
RESET ROLE;
UPDATE mem.scope_grants SET expires_at = NULL
 WHERE to_id = (SELECT prin_a FROM t);
SET LOCAL ROLE memory_app;
SELECT mem.fn_set_scope(tenant_b, prin_b, proj_b, ARRAY[proj_b]) FROM t;

SELECT is(
    (SELECT count(*)::int FROM mem.memories WHERE memory_key = 'pgtap-secret'),
    0, 'another tenant cannot read it even while a valid grant exists elsewhere');

SELECT is(
    (SELECT count(*)::int FROM mem.memories),
    0, 'tenant B sees none of tenant A''s memories at all');

-- The condition the whole model rests on: no scope, no rows. This is the case
-- that catches a connection that skipped fn_set_scope.
SELECT mem.fn_set_scope(NULL, NULL, NULL, NULL);
SELECT is(
    (SELECT count(*)::int FROM mem.memories),
    0, 'a connection with no scope context sees zero rows');

-- Quarantined content is excluded from ordinary retrieval regardless of scope.
RESET ROLE;
INSERT INTO mem.memories
    (id, tenant_id, memory_key, type, title, content, digest, scope_kind,
     project_id, tier, source_type, content_hash, status)
SELECT '00000000-0000-4000-8000-00000000fe03', tenant_a, 'pgtap-quarantined',
       'observation', 'Unreviewed', 'Unreviewed agent content.', 'Unreviewed.',
       'project', proj_a, 'inferred', 'agent', 'pgtap-quar-hash', 'quarantined'
  FROM t
ON CONFLICT DO NOTHING;
SET LOCAL ROLE memory_app;
SELECT mem.fn_set_scope(tenant_a, prin_a, proj_a, ARRAY[proj_a]) FROM t;

-- Asserted as "this specific row is absent", not "nothing came back". The
-- query legitimately matches other memories in the same scope, and an assertion
-- that the result set is empty would pass for the wrong reason the moment
-- retrieval stopped working at all.
SELECT is(
    (SELECT count(*)::int FROM mem.search_hybrid(
        NULL, 'unreviewed agent content', NULL, 'observed',
        now(), NULL, 10, 60) s
      WHERE s.memory_id = '00000000-0000-4000-8000-00000000fe03'),
    0, 'quarantined content is not returned by default retrieval');

SELECT * FROM finish();
ROLLBACK;
