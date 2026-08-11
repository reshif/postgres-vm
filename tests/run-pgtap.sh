#!/usr/bin/env sh
# =============================================================================
# tests/run-pgtap.sh — Suite 2's database half.
#
#   sh tests/run-pgtap.sh
#
# 04-EVALUATION.md §3 specifies Suite 2 "implemented twice, deliberately: pgTAP
# at the database layer and API-level through the real gateway", because "policy
# regressions are silent — no error, just wrong rows". Only the API half
# existed, which meant a refactor that stopped calling db.scoped() would have
# kept the Python suite green while isolation was gone.
#
# Separate from run-all.sh because it runs through psql as the owner rather than
# through the api container, and because it asserts things the Python suite
# structurally cannot — chiefly the grant ladder, which needs privileges the
# application role deliberately does not have.
# =============================================================================
set -eu

cd "$(dirname "$0")/.."

# Git Bash rewrites container-side paths that look like Unix paths. `//pgtap`
# survives that rewriting; `/pgtap` becomes C:/.../pgtap and psql reports a
# missing file that exists.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

if command -v docker >/dev/null 2>&1 && docker version >/dev/null 2>&1; then
  DOCKER=docker
elif command -v docker.exe >/dev/null 2>&1 && docker.exe version >/dev/null 2>&1; then
  DOCKER=docker.exe
else
  echo "Docker CLI is not available" >&2
  exit 1
fi

FAILED=0
for f in ops/pgtap/*.sql; do
  name="$(basename "$f")"
  printf '\n=== pgTAP: %s ===\n' "$name"
  out="$($DOCKER compose exec -T postgres psql -U memory_owner -d memory \
           -f "//pgtap/$name" 2>&1)"
  echo "$out" | grep -E '^ (ok|not ok)' || true

  if echo "$out" | grep -q '^ not ok'; then
    echo "$out" | grep -A 3 '^ not ok'
    FAILED=1
  fi
  # A suite that errors before finishing prints no `not ok` at all, so absence
  # of failures is not the same as success — check that it actually ran.
  if ! echo "$out" | grep -q '^ ok 1'; then
    echo "  SUITE DID NOT RUN:"; echo "$out" | tail -5
    FAILED=1
  fi
done

printf '\n%s\n' "------------------------------------------------------------"
if [ "$FAILED" -ne 0 ]; then
  printf 'pgTAP FAILED\n'; exit 1
fi
printf 'pgTAP passed\n'
