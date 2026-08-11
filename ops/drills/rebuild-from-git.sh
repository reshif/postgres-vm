#!/usr/bin/env sh
# =============================================================================
# ops/drills/rebuild-from-git.sh — prove Plane A survives losing the database.
#
#   REBUILD_CONFIRM=yes sh ops/drills/rebuild-from-git.sh
#
# ADR-0002 splits memory into two planes and rests a lot of weight on the split:
# Plane A is the authoritative knowledge ledger, it lives in git, and the
# database holds a "materialized index" of it. If that is true, deleting every
# Plane A row and re-ingesting restores the corpus exactly. If it is not true,
# the architecture has a claim in it that nobody has ever tested.
#
# This drill tests it destructively, because the non-destructive version — read
# the code and agree it looks right — is what every untested recovery plan is
# made of.
#
# WHAT IT DELETES: git-sourced memories for one project. Nothing else.
# WHAT IT DOES NOT DELETE: Plane B. Episodes, observations, captured failures,
# retrieval telemetry, learned utility, the review queue, the graph built from
# them. Those are NOT in git and this drill does not pretend otherwise — it
# counts them before and after and reports them as what a real disaster would
# have cost. That number is the honest output of the exercise.
#
# BACKUP FIRST. The drill refuses to run without one it can see, and it verifies
# the dump is readable rather than trusting that a file exists — an unreadable
# backup and no backup differ only in how long it takes to find out.
# =============================================================================
set -eu

cd "$(dirname "$0")/../.."

export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

if command -v docker >/dev/null 2>&1; then DOCKER=docker
elif command -v docker.exe >/dev/null 2>&1; then DOCKER=docker.exe
else echo "Docker CLI is not available" >&2; exit 1
fi

TENANT="${MEMORY_DEV_TENANT_ID:-$(grep -E '^MEMORY_DEV_TENANT_ID=' .env | cut -d= -f2)}"
PROJECT="${MEMORY_DEV_PROJECT_ID:-$(grep -E '^MEMORY_DEV_PROJECT_ID=' .env | cut -d= -f2)}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"

psql_owner() {
  $DOCKER compose exec -T postgres psql -U memory_owner -d memory -tAc "$1"
}

say() { printf '%s\n' "$1"; }
rule() { printf '%s\n' "------------------------------------------------------------"; }

say "Rebuild-from-git drill"
say "  tenant  $TENANT"
say "  project $PROJECT"
rule

# ------------------------------------------------------------------ guard
if [ "${REBUILD_CONFIRM:-}" != "yes" ]; then
  cat >&2 <<'MSG'
This drill DELETES every git-sourced memory for the project and rebuilds it from
the repository. That is the point, and it is still deletion.

Re-run with REBUILD_CONFIRM=yes once you have read what it does above.
MSG
  exit 2
fi

# --------------------------------------------------------------- backup gate
say "1. Backup"
# backup.sh writes one timestamped DIRECTORY per run, holding memory.dump, its
# table of contents, and the Prometheus and Loki archives — not a flat
# postgres-*.dump. Globbing for the wrong shape reports "no backup" while a
# perfectly good one sits on disk, which in a drill about recoverability would be
# an unusually poor failure to ship.
backup_dir="$($DOCKER compose run --rm --entrypoint sh backup -c \
  "ls -1td $BACKUP_DIR/*/ 2>/dev/null | head -1" 2>/dev/null | tr -d '\r' | tail -1)"
latest="${backup_dir}memory.dump"
if [ -z "$backup_dir" ]; then
  echo "  no postgres dump found in $BACKUP_DIR — run: docker compose run --rm backup" >&2
  exit 3
fi
say "  found $latest"

# `pg_restore --list` parses the archive's table of contents. A dump truncated by
# a full disk still exists and still has a plausible size; it fails here.
if $DOCKER compose run --rm --entrypoint sh backup -c \
     "pg_restore --list '$latest' > /dev/null 2>&1"; then
  say "  the dump is readable (pg_restore --list parsed its table of contents)"
else
  echo "  THE BACKUP IS NOT READABLE. Refusing to continue." >&2
  exit 3
fi

# ------------------------------------------------------------------ before
say ""
say "2. State before"
before_a="$(psql_owner "SELECT count(*) FROM mem.memories
   WHERE tenant_id='$TENANT' AND project_id='$PROJECT' AND source_type='git'")"
before_b="$(psql_owner "SELECT count(*) FROM mem.memories
   WHERE tenant_id='$TENANT' AND project_id='$PROJECT' AND source_type<>'git'")"
# The fingerprint is what makes "restored exactly" checkable rather than
# assertable: same keys AND same content, order-independent.
before_fp="$(psql_owner "SELECT md5(string_agg(memory_key||':'||content_hash, ',' ORDER BY memory_key))
   FROM mem.memories
   WHERE tenant_id='$TENANT' AND project_id='$PROJECT'
     AND source_type='git' AND upper(valid_at) IS NULL")"
say "  Plane A (git-sourced) memories: $before_a"
say "  Plane B memories:               $before_b"
say "  Plane A fingerprint:            $before_fp"

# ------------------------------------------------------------------ destroy
say ""
say "3. Destroy Plane A"
# Deleted as the OWNER on purpose. memory_app holds no DELETE grant anywhere —
# that is a deliberate property of the role (ADR-0007), so a drill that could run
# as the application role would be evidence of a different bug.
deleted="$(psql_owner "WITH gone AS (
     DELETE FROM mem.memories
      WHERE tenant_id='$TENANT' AND project_id='$PROJECT' AND source_type='git'
      RETURNING 1)
   SELECT count(*) FROM gone")"
say "  deleted $deleted git-sourced memories"

remaining="$(psql_owner "SELECT count(*) FROM mem.memories
   WHERE tenant_id='$TENANT' AND project_id='$PROJECT' AND source_type='git'")"
if [ "$remaining" != "0" ]; then
  echo "  deletion did not take — $remaining rows remain; aborting" >&2
  exit 4
fi
say "  verified: no git-sourced memories remain"

# ------------------------------------------------------------------ rebuild
say ""
say "4. Rebuild from the repository"

# The API's connection pool is disturbed by the backup step above (it waits on
# postgres, and a health probe can recycle connections). Rebuilding through a
# half-ready API is how the first version of this drill reported a catastrophic
# empty rebuild: the POST failed, `|| echo ''` swallowed the error, and the drill
# went on to announce that Plane A had not come back — a false alarm about data
# loss, produced by a drill whose entire job is to be trusted about data loss.
say "  waiting for the API to be ready"
ready=0
i=0
while [ "$i" -lt 30 ]; do
  if $DOCKER compose exec -T api curl -fsS -o /dev/null localhost:8080/readyz 2>/dev/null; then
    ready=1; break
  fi
  i=$((i + 1))
  sleep 2
done
if [ "$ready" -ne 1 ]; then
  echo "  the API never became ready; refusing to report a rebuild result" >&2
  echo "  Plane A is currently EMPTY. Re-ingest or restore from $latest" >&2
  exit 5
fi

rebuild="$($DOCKER compose exec -T api curl -sS -w '\n%{http_code}' \
  -X POST localhost:8080/v1/ingest \
  -H 'content-type: application/json' \
  -d "{\"tenant_id\":\"$TENANT\",\"project_id\":\"$PROJECT\"}" 2>&1)"
status="$(printf '%s' "$rebuild" | tail -1 | tr -d '\r')"
body="$(printf '%s' "$rebuild" | sed '$d')"
say "  ingest HTTP $status: $(printf '%s' "$body" | cut -c1-160)"
if [ "$status" != "200" ]; then
  echo "  THE REBUILD REQUEST FAILED. Plane A is currently EMPTY." >&2
  echo "  Fix the API and re-run, or restore from $latest" >&2
  exit 5
fi

# ------------------------------------------------------------------- after
say ""
say "5. State after"
after_a="$(psql_owner "SELECT count(*) FROM mem.memories
   WHERE tenant_id='$TENANT' AND project_id='$PROJECT' AND source_type='git'")"
after_b="$(psql_owner "SELECT count(*) FROM mem.memories
   WHERE tenant_id='$TENANT' AND project_id='$PROJECT' AND source_type<>'git'")"
after_fp="$(psql_owner "SELECT md5(string_agg(memory_key||':'||content_hash, ',' ORDER BY memory_key))
   FROM mem.memories
   WHERE tenant_id='$TENANT' AND project_id='$PROJECT'
     AND source_type='git' AND upper(valid_at) IS NULL")"
embedded="$(psql_owner "SELECT count(*) FROM mem.memories m
   JOIN mem.memory_embeddings e ON e.memory_id = m.id
   WHERE m.tenant_id='$TENANT' AND m.project_id='$PROJECT' AND m.source_type='git'")"
say "  Plane A (git-sourced) memories: $after_a"
say "  Plane B memories:               $after_b"
say "  Plane A fingerprint:            $after_fp"
say "  re-embedded:                    $embedded / $after_a"

# ------------------------------------------------------------------ verdict
say ""
rule
FAILED=0
if [ "$before_fp" = "$after_fp" ]; then
  say "PASS  Plane A restored EXACTLY — same keys, same content hashes"
else
  say "FAIL  Plane A did not come back identical"
  say "      before $before_fp"
  say "      after  $after_fp"
  FAILED=1
fi

if [ "$embedded" = "$after_a" ]; then
  say "PASS  every restored memory is embedded and retrievable"
else
  say "WARN  $embedded of $after_a restored memories carry a vector"
  say "      the rest are lexically searchable and will be backfilled by the"
  say "      maintenance sweep; a cold embedder during the rebuild does this"
fi

if [ "$before_b" = "$after_b" ]; then
  say "PASS  Plane B was untouched by the drill ($after_b memories)"
else
  say "FAIL  Plane B changed: $before_b -> $after_b. The drill damaged something"
  say "      it does not own."
  FAILED=1
fi

# The finding the first run of this drill produced, and the reason the row count
# and the fingerprint are reported separately.
#
# The fingerprint matched exactly while the row count fell from 32 to 24. Both
# are true: the fingerprint covers currently-valid rows, and every one of those
# came back byte-identical. The missing 8 were SUPERSEDED versions — the previous
# contents of files that have been edited since.
#
# Git holds that history in its own log, but ingestion replays one commit, so the
# rebuilt database knows only the present. ADR-0006 makes the model bi-temporal
# precisely so "what did we believe in June" stays answerable, and after a
# rebuild-from-git it is not. Restoring it needs the dump.
if [ "$before_a" != "$after_a" ]; then
  lost=$((before_a - after_a))
  say ""
  say "NOTE  $before_a rows became $after_a while the fingerprint held."
  say "      $lost superseded version(s) were not restored. A rebuild replays the"
  say "      current commit, so the corpus is correct and its HISTORY is gone —"
  say "      as-of queries before today's content will find nothing. This is a"
  say "      real limit of the two-plane claim, not a failure of this run."
fi

say ""
say "WHAT A REAL LOSS WOULD HAVE COST, that git cannot return:"
say "  $before_b Plane B memories (episodes, captures, observations)"
psql_owner "SELECT '  ' || count(*) || ' retrieval events (utility evidence)'
   FROM mem.retrieval_events WHERE tenant_id='$TENANT'" || true
psql_owner "SELECT '  ' || count(*) || ' review-queue items'
   FROM mem.memories WHERE tenant_id='$TENANT' AND project_id='$PROJECT'
     AND status='quarantined'" || true
say "  Restoring those needs the pg_dump above, not the repository. That is the"
say "  drill's real finding: git rebuilds the knowledge, not the experience."

rule
if [ "$FAILED" -ne 0 ]; then
  say "REBUILD DRILL FAILED"
  say "Restore from $latest before continuing:"
  say "  RESTORE_CONFIRM=yes sh ops/backup/restore.sh $latest"
  exit 1
fi
say "REBUILD DRILL PASSED"
