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
# Requires: a .env file with DB_PGBOUNCER_PASSWORD.
# =============================================================================
set -eu

cd "$(dirname "$0")/../.."
[ -f .env ] || { echo "no .env — copy .env.example first" >&2; exit 1; }
# shellcheck disable=SC1091
. ./.env

: "${DB_PGBOUNCER_PASSWORD:?DB_PGBOUNCER_PASSWORD not set in .env}"

OUT=ops/pgbouncer/userlist.txt

# This MUST be the plaintext password, not a SCRAM verifier.
#
# A stored SCRAM verifier is not a credential you can authenticate *with* — it is
# what the server checks a live SCRAM handshake against. PgBouncer can forward a
# verifier to Postgres only via SCRAM pass-through, where the client's own
# handshake supplies the ClientKey. The auth_user connection has no client behind
# it: PgBouncer opens it itself to run auth_query. So a verifier here fails with
#
#   ERROR ... cannot do SCRAM authentication: password is SCRAM secret but
#             client authentication did not provide SCRAM keys
#   LOG   ... closing because: server login failed: wrong password type
#
# which surfaces to the API as an opaque "wrong password type" on /readyz.
#
# This is not a downgrade in security posture. Client auth is still
# scram-sha-256, and every application password still stays out of this file —
# they are resolved through auth_query against pg_authid. The single credential
# written here belongs to pgbouncer_auth, a NOLOGIN-to-your-data role whose only
# privilege is EXECUTE on pgbouncer.get_auth(). Keep the file at 0600.
printf '"pgbouncer_auth" "%s"\n' "$DB_PGBOUNCER_PASSWORD" > "$OUT"

chmod 600 "$OUT"
echo "wrote $OUT (one credential: pgbouncer_auth)"
echo "reminder: userlist.txt is gitignored — do not commit it"
