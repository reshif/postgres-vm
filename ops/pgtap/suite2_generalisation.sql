-- =============================================================================
-- Suite 2, database half: the generalisation exclusion for restricted material.
--
-- ADR-0012: memories classified `restricted` are "permanently excluded from
-- generalisation, not merely excluded by default".
--
-- This lives in pgTAP rather than in tests/test_org_entities.py because the
-- Python suite structurally cannot build the fixture. Classifying a memory
-- `restricted` requires a scope grant carrying a max_sensitivity ceiling, and
-- mem.scope_grants has a read policy and NO insert policy — the application role
-- cannot grant itself elevated sensitivity by any route. That is the correct
-- design, and it is exactly why this assertion needs the owner.
--
-- The property under test is a specific silent failure. The exclusion check was
-- first written as an ordinary count over entity_mentions joined to
-- mem.memories, and it returned 0 for every entity, because the sensitivity
-- policy from 0023 hides restricted rows from a session with no grant for them.
-- The safeguard was blind to precisely the material it exists to catch, and it
-- failed PERMISSIVE: an entity backed only by restricted content screened clean.
-- mem.entity_restricted_support is SECURITY DEFINER so it can see what the
-- caller may not, and returns a bare count so the caller learns that promotion
-- is blocked and nothing about what is being protected.
-- =============================================================================
BEGIN;
SELECT plan(7);

CREATE TEMP TABLE fixture AS
SELECT
  'd0000000-0000-0000-0000-0000000000f1'::uuid AS tenant,
  'd0000000-0000-0000-0000-0000000000f2'::uuid AS project,
  'd0000000-0000-0000-0000-0000000000f3'::uuid AS principal,
  'd0000000-0000-0000-0000-0000000000f4'::uuid AS restricted_entity,
  'd0000000-0000-0000-0000-0000000000f5'::uuid AS clean_entity,
  'd0000000-0000-0000-0000-0000000000f6'::uuid AS restricted_memory,
  'd0000000-0000-0000-0000-0000000000f7'::uuid AS clean_memory;

INSERT INTO mem.organizations (id, slug, name)
SELECT tenant, 'pgtap-gen', 'pgTAP generalisation' FROM fixture
ON CONFLICT DO NOTHING;

INSERT INTO mem.projects (id, tenant_id, slug, name)
SELECT project, tenant, 'pgtap-gen-p', 'pgTAP generalisation' FROM fixture
ON CONFLICT DO NOTHING;

INSERT INTO mem.principals (id, tenant_id, actor, external_id, display_name)
SELECT principal, tenant, 'human', 'pgtap-gen', 'pgTAP' FROM fixture
ON CONFLICT DO NOTHING;

INSERT INTO mem.entities (id, tenant_id, project_id, kind, canonical_name, tier)
SELECT restricted_entity, tenant, project, 'technology', 'PgtapVault', 'observed'
  FROM fixture
ON CONFLICT DO NOTHING;

INSERT INTO mem.entities (id, tenant_id, project_id, kind, canonical_name, tier)
SELECT clean_entity, tenant, project, 'technology', 'PgtapOpenThing', 'observed'
  FROM fixture
ON CONFLICT DO NOTHING;

-- Two memories on the same project: one restricted, one internal.
INSERT INTO mem.memories
  (id, tenant_id, memory_key, type, title, content, digest, scope_kind,
   project_id, tier, confidence, source_type, status, token_cost, content_hash,
   sensitivity)
SELECT restricted_memory, tenant, 'pgtap:gen:restricted', 'observation',
       'Restricted note', 'Signing key rotation cadence for pgtap fixture.',
       'Signing key rotation cadence for pgtap fixture.', 'project', project,
       'authoritative', 0.95, 'human', 'active', 10,
       'pgtapgenrestricted0000000000000000000000000000000000000000000001',
       'restricted'
  FROM fixture
ON CONFLICT DO NOTHING;

INSERT INTO mem.memories
  (id, tenant_id, memory_key, type, title, content, digest, scope_kind,
   project_id, tier, confidence, source_type, status, token_cost, content_hash,
   sensitivity)
SELECT clean_memory, tenant, 'pgtap:gen:clean', 'observation',
       'Ordinary note', 'An unremarkable internal note for the pgtap fixture.',
       'An unremarkable internal note for the pgtap fixture.', 'project', project,
       'authoritative', 0.95, 'human', 'active', 10,
       'pgtapgenclean000000000000000000000000000000000000000000000000002',
       'internal'
  FROM fixture
ON CONFLICT DO NOTHING;

INSERT INTO mem.entity_mentions (tenant_id, entity_id, memory_id)
SELECT tenant, restricted_entity, restricted_memory FROM fixture
ON CONFLICT DO NOTHING;

INSERT INTO mem.entity_mentions (tenant_id, entity_id, memory_id)
SELECT tenant, clean_entity, clean_memory FROM fixture
ON CONFLICT DO NOTHING;

-- The function is scope-aware, so it has to run under a scope.
SELECT mem.fn_set_scope(tenant, principal, project, ARRAY[project]) FROM fixture;

SELECT ok(
  mem.entity_restricted_support((SELECT restricted_entity FROM fixture)) > 0,
  'an entity backed by restricted material reports restricted support');

SELECT is(
  mem.entity_restricted_support((SELECT clean_entity FROM fixture)), 0,
  'an entity backed only by internal material reports none');

-- The point of the whole exercise: the ordinary count a caller would write is
-- blind here, and the function is not.
SELECT is(
  (SELECT count(*)::integer
     FROM mem.entity_mentions em
     JOIN mem.memories m ON m.id = em.memory_id
    WHERE em.entity_id = (SELECT restricted_entity FROM fixture)
      AND m.sensitivity = 'restricted'),
  1,
  'the owner can see the restricted mention directly (fixture is real)');

SELECT ok(
  (SELECT max_sensitivity IS NULL FROM mem.scope_grants
    WHERE tenant_id = (SELECT tenant FROM fixture) LIMIT 1) IS NOT FALSE,
  'no sensitivity ceiling has been granted to this fixture tenant');

-- Now prove the blindness that made this function necessary, from the
-- application role's own perspective. The temp fixture table belongs to the
-- owner, so the switched role needs to be able to read it — otherwise the
-- suite fails on the scaffolding rather than on the property.
GRANT SELECT ON fixture TO memory_app;
SET LOCAL ROLE memory_app;
SELECT mem.fn_set_scope(tenant, principal, project, ARRAY[project]) FROM fixture;

SELECT is(
  (SELECT count(*)::integer FROM mem.memories
    WHERE id = (SELECT restricted_memory FROM fixture)),
  0,
  'memory_app cannot see the restricted memory at all');

SELECT is(
  (SELECT count(*)::integer
     FROM mem.entity_mentions em
     JOIN mem.memories m ON m.id = em.memory_id
    WHERE em.entity_id = (SELECT restricted_entity FROM fixture)
      AND m.sensitivity = 'restricted'),
  0,
  'so a plain count returns zero — the silent, permissive failure');

SELECT ok(
  mem.entity_restricted_support((SELECT restricted_entity FROM fixture)) > 0,
  'but the SECURITY DEFINER check still blocks the promotion');

RESET ROLE;
SELECT * FROM finish();
ROLLBACK;
