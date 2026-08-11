#!/usr/bin/env sh
# =============================================================================
# tests/run-all.sh — run every suite against a running stack.
#
#   docker compose up -d --wait
#   sh tests/run-all.sh
#
# The suites are piped into the api container rather than mounted, so no image
# rebuild is needed to run a test you just edited, and CI does not need a second
# Python environment with the project's dependencies installed.
#
# Exits non-zero if any suite fails, so it works as a CI gate unchanged.
# =============================================================================
set -eu

cd "$(dirname "$0")/.."

# Docker Desktop may be exposed only as docker.exe inside WSL. Probe the daemon
# rather than trusting a stub binary on PATH.
if command -v docker >/dev/null 2>&1 && docker version >/dev/null 2>&1; then
  DOCKER=docker
elif command -v docker.exe >/dev/null 2>&1 && docker.exe version >/dev/null 2>&1; then
  DOCKER=docker.exe
else
  echo "Docker CLI is not available" >&2
  exit 1
fi

SUITES="test_rls_coverage test_isolation test_write_path test_ingest test_context test_planner test_evidence test_hybrid_lexical test_capture test_binding test_github_evidence test_github_client test_evidence_assertions test_github_sidecar_sync test_cli test_entities test_injection test_conflicts test_inbox test_extraction test_temporal test_console_data test_evaluations test_maintenance test_consolidation test_distillation test_arms test_org_entities test_limits test_auth test_mcp test_mcp_extensions test_eval_snapshot test_eval_cases test_eval_export test_eval_latency_gate test_observability"
FAILED=""

# Ordered deliberately: RLS coverage runs FIRST. It is the structural check, and
# if isolation is broken every other result is meaningless — a green write-path
# suite on a leaking database is worse than no result, because it reads as
# reassurance.
for s in $SUITES; do
  printf '\n=== %s ===\n' "$s"
  if $DOCKER compose exec -T api python - < "tests/$s.py"; then
    :
  else
    FAILED="$FAILED $s"
  fi
done

printf '\n%s\n' "------------------------------------------------------------"

# ---------------------------------------------------------------------------
# Record the isolation result, pass OR FAIL.
#
# 04-EVALUATION.md §7.3 gates production on "Suite 2 (isolation) at 100% for 30
# consecutive days". Passing today is not that claim, and nothing was retaining
# the history that would let anyone make it — the go/no-go could only ever report
# it as unmeasured.
#
# Recorded on BOTH paths deliberately. A history that only writes rows when the
# suites pass would show an unbroken green streak no matter how often isolation
# broke, which is worse than no history: it would look like evidence.
ISO_STATUS=passed
case "$FAILED" in
  *test_rls_coverage*|*test_isolation*) ISO_STATUS=failed ;;
esac

DEV_TENANT="${MEMORY_DEV_TENANT_ID:-$(grep -E '^MEMORY_DEV_TENANT_ID=' .env 2>/dev/null | cut -d= -f2 | tr -d '\r')}"
DEV_PROJECT="${MEMORY_DEV_PROJECT_ID:-$(grep -E '^MEMORY_DEV_PROJECT_ID=' .env 2>/dev/null | cut -d= -f2 | tr -d '\r')}"
if [ -n "$DEV_TENANT" ] && [ -n "$DEV_PROJECT" ]; then
  $DOCKER compose exec -T postgres psql -U memory_owner -d memory -q -c \
    "INSERT INTO mem.evaluation_runs
       (tenant_id, project_id, suite, status, metrics, completed_at)
     VALUES ('$DEV_TENANT', '$DEV_PROJECT', 'isolation', '$ISO_STATUS',
             '{\"suites\": [\"test_rls_coverage\", \"test_isolation\"],
               \"note\": \"pgTAP half runs separately via tests/run-pgtap.sh\"}'::jsonb,
             now());" >/dev/null 2>&1 \
    && printf 'isolation result recorded (%s)\n' "$ISO_STATUS" \
    || printf 'could not record the isolation result\n'
fi

if [ -n "$FAILED" ]; then
  printf 'FAILED:%s\n' "$FAILED"
  exit 1
fi
printf 'all suites passed\n'

# Clean up the fixture tenants. memory_app holds no DELETE grant by design, so
# this is an owner-side admin operation.
$DOCKER compose exec -T postgres psql -U memory_owner -d memory -q -c \
  "DELETE FROM mem.organizations WHERE slug IN
   ('tenant-a','tenant-b','tenant-c','leak-a','leak-b','ing','ctx','cap','ent','redteam','conf','ibx','temporal','maint','llmx')
   OR slug LIKE 'auth-%' OR slug LIKE 'cli-%' OR slug LIKE 'console-test-%' OR slug LIKE 'console-%' OR slug LIKE 'eval-ui-%' OR slug LIKE 'mcp-%' OR slug LIKE 'hybrid-%';
   DELETE FROM mem.memories WHERE memory_key LIKE 'mcp-cross-client:%'
      OR content LIKE '%mcp-cross-client-%'
      OR content LIKE '%mcp-fixture-%';
   DELETE FROM mem.organizations WHERE slug IN ('consol','distill','orgent','armtest');
   DELETE FROM mem.projects WHERE slug LIKE 'consol-p-%' OR slug LIKE 'distill-%'
      OR slug LIKE 'orgent-%' OR slug LIKE 'arms-%' OR slug LIKE 'lonely-%';" \
  >/dev/null 2>&1 || true

# WHY THE CONTENT MATCH ABOVE, NOT JUST THE KEY.
#
# test_mcp writes most of its fixtures through the memory_write TOOL, which does
# not accept a memory_key — the server derives one from the content hash
# (`agent:<hash>`). So `memory_key LIKE 'mcp-cross-client:%'` matched exactly one
# row per run and left the other five behind, quarantined, forever. The marker
# those rows carry is in their CONTENT.
#
# That leak is not cosmetic. Quarantined rows are the review inbox, so every run
# of this suite raised the inbox depth permanently: 54 of the 66 items sitting in
# it were this, against 8 real ones. ADR-0015 alerts at depth 100 and the §7.3
# production gate wants a 14-day median under 40 — both of which this would have
# tripped, reporting a curation backlog that did not exist and burying the eight
# items a curator actually needed to see.
