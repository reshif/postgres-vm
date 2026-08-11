#!/usr/bin/env sh
# =============================================================================
# Backups for the three stores that hold state worth losing sleep over.
#
# Volumes survive a restart. They do not survive a deleted volume, a corrupted
# filesystem, or a laptop. This was the only remaining gap in the platform where
# failure is UNRECOVERABLE rather than merely disruptive.
#
#   docker compose run --rm backup            # one-off
#   docker compose --profile backup up -d     # scheduled, see the `backup` service
#
# WHAT IS BACKED UP, AND WHY EACH
#
#   postgres    Everything that matters. Memories, the graph, audit log, the
#               review queue. `pg_dump -Fc` because a custom-format dump can be
#               restored selectively and in parallel; a plain SQL file cannot.
#   prometheus  The gates in §7.3 are multi-day windows ("p95 < 350 ms for 7
#               days", "Suite 2 at 100% for 30 days"). Losing the TSDB does not
#               lose data so much as RESET THE CLOCK on every gate.
#   loki        Log history is what reconstructs an incident weeks later, which
#               is precisely when nobody still has the terminal open.
#
# WHAT IS NOT BACKED UP: Tempo. Traces are for debugging something happening
# now, they expire in 48h by design, and re-creating them costs one request.
# Backing them up would be paying to store something already declared disposable.
# =============================================================================
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${BACKUP_DIR}/${STAMP}"

mkdir -p "$DEST"
echo "backup -> $DEST"

# ---------------------------------------------------------------- postgres
# --clean --if-exists so a restore into a non-empty database is deterministic
# rather than a pile of "already exists" errors that hide the real failure.
echo "  postgres: dumping..."
PGPASSWORD="${DB_OWNER_PASSWORD:?set DB_OWNER_PASSWORD}" \
  pg_dump \
    --host="${DB_HOST:-postgres}" \
    --username="${DB_OWNER_USER:-memory_owner}" \
    --dbname="${DB_NAME:-memory}" \
    --format=custom \
    --compress=6 \
    --clean --if-exists \
    --file="${DEST}/memory.dump"

# Verified, not assumed. A dump that cannot be listed is not a backup, and the
# time to discover that is now rather than during a restore.
pg_restore --list "${DEST}/memory.dump" > "${DEST}/memory.toc"
echo "  postgres: $(wc -l < "${DEST}/memory.toc") objects in the dump"

# -------------------------------------------------------------- prometheus
# The admin API snapshots the TSDB consistently; copying the directory under a
# running Prometheus can capture a half-written block. Requires
# --web.enable-admin-api, which is off by default and off in prod unless set.
echo "  prometheus: snapshotting..."
# curl, not wget: the base image ships neither, and this script previously
# reported "admin API unavailable" when the truth was "no HTTP client in this
# container" — a missing tool disguised as a missing feature.
if SNAP="$(curl -sf -XPOST "http://${PROM_HOST:-prometheus}:9090/api/v1/admin/tsdb/snapshot" 2>/dev/null)"; then
    NAME="$(echo "$SNAP" | sed -n 's/.*"name":"\([^"]*\)".*/\1/p')"
    if [ -n "$NAME" ] && [ -d "/prometheus/snapshots/$NAME" ]; then
        tar -czf "${DEST}/prometheus-${NAME}.tar.gz" -C /prometheus/snapshots "$NAME"
        rm -rf "/prometheus/snapshots/${NAME:?}"
        echo "  prometheus: snapshot ${NAME} archived"
    else
        echo "  prometheus: snapshot API returned no usable name; skipped"
    fi
else
    echo "  prometheus: admin API unavailable (needs --web.enable-admin-api); skipped"
fi

# -------------------------------------------------------------------- loki
# Loki's filesystem store is chunks plus an index. There is no snapshot API for
# the single-binary deployment, so this is a file copy — acceptable because a
# torn chunk costs some recent log lines, not the store.
echo "  loki: archiving chunks..."
if [ -d /loki ]; then
    tar -czf "${DEST}/loki.tar.gz" -C / loki 2>/dev/null || \
        echo "  loki: archive incomplete (files changed mid-copy)"
    echo "  loki: $(du -sh "${DEST}/loki.tar.gz" 2>/dev/null | cut -f1) archived"
else
    echo "  loki: /loki not mounted; skipped"
fi

# ------------------------------------------------------------------ prune
# Deleting old backups is part of backing up: a disk that fills stops the
# backups AND the database sharing the volume.
find "$BACKUP_DIR" -maxdepth 1 -type d -name '20*' -mtime "+${KEEP_DAYS}" \
     -exec rm -rf {} + 2>/dev/null || true

echo "backup complete: $(du -sh "$DEST" | cut -f1) in $DEST"
echo "restore with: ops/backup/restore.sh $STAMP"
