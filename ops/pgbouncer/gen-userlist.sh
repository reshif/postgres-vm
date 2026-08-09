#!/usr/bin/env sh
# =============================================================================
# ops/pgbouncer/gen-userlist.sh
#
# Writes ops/pgbouncer/userlist.txt containing exactly ONE credential: the
# pgbouncer_auth role. Every other user is authenticated via auth_query against
# Postgres (see ops/initdb/01-roles.sql), so application passwords never appear
# in this file.
#
# Run once before the first `docker compose up`, and again whenever you rotate
# DB_PGBOUNCER_PASSWORD.
#
#   sh ops/pgbouncer/gen-userlist.sh
#
# Requires: a .env file with DB_PGBOUNCER_PASSWORD, and either python3 (preferred,
# generates a SCRAM verifier) or a running Postgres container (fallback).
# =============================================================================
set -eu

cd "$(dirname "$0")/../.."
[ -f .env ] || { echo "no .env — copy .env.example first" >&2; exit 1; }
# shellcheck disable=SC1091
. ./.env

: "${DB_PGBOUNCER_PASSWORD:?DB_PGBOUNCER_PASSWORD not set in .env}"

OUT=ops/pgbouncer/userlist.txt

# PgBouncer accepts a plaintext password in userlist.txt and derives what it needs
# for scram-sha-256 client auth. That is fine for a local file with 0600 perms and
# one non-privileged credential. If you prefer a stored verifier (recommended for
# any shared host), use the SCRAM branch below.
if command -v python3 >/dev/null 2>&1; then
  python3 - "$DB_PGBOUNCER_PASSWORD" > "$OUT" <<'PY'
import base64, hashlib, hmac, os, sys

# Produces the same SCRAM-SHA-256 verifier format Postgres stores, so the file
# contains no recoverable plaintext.
password = sys.argv[1].encode()
salt = os.urandom(16)
iterations = 4096
salted = hashlib.pbkdf2_hmac("sha256", password, salt, iterations)
client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
stored_key = hashlib.sha256(client_key).digest()
server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
verifier = "SCRAM-SHA-256${}:{}${}:{}".format(
    iterations,
    base64.b64encode(salt).decode(),
    base64.b64encode(stored_key).decode(),
    base64.b64encode(server_key).decode(),
)
print('"pgbouncer_auth" "{}"'.format(verifier))
PY
else
  printf '"pgbouncer_auth" "%s"\n' "$DB_PGBOUNCER_PASSWORD" > "$OUT"
  echo "WARNING: python3 not found — wrote a plaintext password. Fine for a laptop," >&2
  echo "         not for anything shared. Regenerate with python3 available." >&2
fi

chmod 600 "$OUT"
echo "wrote $OUT (one credential: pgbouncer_auth)"
echo "reminder: userlist.txt is gitignored — do not commit it"
