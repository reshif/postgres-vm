#!/usr/bin/env sh
# =============================================================================
# Restore from a backup taken by backup.sh.
#
#   docker compose run --rm backup /restore.sh 20260811T031500Z
#
# THIS SCRIPT EXISTS BECAUSE A BACKUP NOBODY HAS RESTORED IS A HYPOTHESIS.
# 05-BUILD-PLAN Phase 9 asks for a quarterly rebuild-from-git drill for exactly
# this reason. Writing the restore path at the same time as the backup path is
# what makes the drill possible; discovering the restore does not work during an
# incident is the normal way this fails.
#
# Postgres restore is DESTRUCTIVE and refuses to run without an explicit
# confirmation, because "restore the wrong snapshot over a working database" is
# a worse outcome than the outage that prompted it.
# =============================================================================
set -eu

STAMP="${1:?usage: restore.sh <UTC-stamp>  (see ls /backups)}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
SRC="${BACKUP_DIR}/${STAMP}"

[ -d "$SRC" ] || { echo "no backup at $SRC"; ls -1 "$BACKUP_DIR" 2>/dev/null; exit 1; }

echo "restoring from $SRC"
echo
echo "  This OVERWRITES the current database."
if [ "${RESTORE_CONFIRM:-}" != "yes" ]; then
    echo "  Refusing without RESTORE_CONFIRM=yes."
    echo "  Re-run:  RESTORE_CONFIRM=yes ops/backup/restore.sh $STAMP"
    exit 2
fi

# --clean --if-exists is already baked into the dump; --single-transaction makes
# the restore all-or-nothing, so a failure halfway leaves the previous database
# intact rather than a half-replaced one.
PGPASSWORD="${DB_OWNER_PASSWORD:?set DB_OWNER_PASSWORD}" \
  pg_restore \
    --host="${DB_HOST:-postgres}" \
    --username="${DB_OWNER_USER:-memory_owner}" \
    --dbname="${DB_NAME:-memory}" \
    --single-transaction \
    --exit-on-error \
    "${SRC}/memory.dump"

echo "postgres restored."
echo
echo "Prometheus and Loki archives are NOT restored automatically: both stores"
echo "must be stopped while their data directory is replaced, and doing that"
echo "from inside a running stack is how you get a corrupted store on top of an"
echo "outage. To restore them:"
echo "  docker compose stop prometheus loki"
echo "  tar -xzf ${SRC}/prometheus-*.tar.gz -C <prometheus volume>"
echo "  tar -xzf ${SRC}/loki.tar.gz -C /"
echo "  docker compose start prometheus loki"
