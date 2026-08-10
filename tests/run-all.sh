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

SUITES="test_rls_coverage test_isolation test_write_path test_ingest test_context test_planner test_capture test_binding test_entities test_injection test_conflicts test_inbox test_extraction test_temporal test_maintenance test_limits test_auth test_eval_snapshot"
FAILED=""

# Ordered deliberately: RLS coverage runs FIRST. It is the structural check, and
# if isolation is broken every other result is meaningless — a green write-path
# suite on a leaking database is worse than no result, because it reads as
# reassurance.
for s in $SUITES; do
  printf '\n=== %s ===\n' "$s"
  if docker compose exec -T api python - < "tests/$s.py"; then
    :
  else
    FAILED="$FAILED $s"
  fi
done

printf '\n%s\n' "------------------------------------------------------------"
if [ -n "$FAILED" ]; then
  printf 'FAILED:%s\n' "$FAILED"
  exit 1
fi
printf 'all suites passed\n'

# Clean up the fixture tenants. memory_app holds no DELETE grant by design, so
# this is an owner-side admin operation.
docker compose exec -T postgres psql -U memory_owner -d memory -q -c \
  "DELETE FROM mem.organizations WHERE slug IN
   ('tenant-a','tenant-b','tenant-c','leak-a','leak-b','ing','ctx','cap','ent','redteam','conf','ibx','temporal','maint','llmx')
   OR slug LIKE 'auth-%' OR slug LIKE 'console-test-%';" >/dev/null 2>&1 || true
