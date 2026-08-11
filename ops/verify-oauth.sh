#!/usr/bin/env sh
# =============================================================================
# ops/verify-oauth.sh — prove the auth path against a REAL identity provider.
#
#   docker compose --profile auth up -d keycloak
#   sh ops/verify-oauth.sh
#
# WHY THIS EXISTS WHEN tests/test_auth.py ALREADY PASSES.
#
# That suite proves the hard cryptographic parts — `alg: none`, an HS256 token
# signed with the public key, expiry, audience, issuer, and that a forged project
# claim is refused before any handler runs. It does it by injecting a
# verification key directly, which is what makes it fast and exhaustive.
#
# It also means it never resolves an endpoint, never parses a discovery document
# and never sees a claim an actual provider chose to emit. The first time this
# stack was pointed at a real Keycloak, every single request failed with
# `MEMORY_OAUTH_JWKS_URL is not configured` — the code required an operator to
# hand-copy the JWKS URL and did no OIDC discovery at all. A synthetic-key suite
# could not have found that, and it would have been found in production instead.
#
# So this script is the complement: fewer cases, all of them against a provider
# that serves discovery, rotates real keys, and issues real tokens.
# =============================================================================
set -eu

cd "$(dirname "$0")/.."

KC="${KEYCLOAK_URL:-http://localhost:8090}"
REALM="${KEYCLOAK_REALM:-memory-platform}"
CLIENT="${KEYCLOAK_CLIENT:-memory-console}"
API="${API_URL:-http://localhost:8080}"
TOKEN_URL="$KC/realms/$REALM/protocol/openid-connect/token"

TENANT="${MEMORY_DEV_TENANT_ID:-$(grep -E '^MEMORY_DEV_TENANT_ID=' .env 2>/dev/null | cut -d= -f2 | tr -d '\r')}"
PROJECT="${MEMORY_DEV_PROJECT_ID:-$(grep -E '^MEMORY_DEV_PROJECT_ID=' .env 2>/dev/null | cut -d= -f2 | tr -d '\r')}"

FAILED=0
pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s  [%s]\n' "$1" "${2:-}"; FAILED=1; }

expect() { # expect <label> <wanted-status> <actual-status>
  if [ "$2" = "$3" ]; then pass "$1 -> $3"; else fail "$1" "wanted $2, got $3"; fi
}

mint() {
  curl -s -X POST "$TOKEN_URL" \
    -d grant_type=password -d "client_id=$CLIENT" \
    -d "username=$1" -d "password=$1-password" -d scope=openid \
  | python -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))'
}

call() { # call <token> <tenant> <project>
  curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $1" \
    "$API/v1/search?q=postgres&limit=2&tenant_id=$2&project_id=$3"
}

printf 'OAuth end-to-end against %s\n' "$KC"
printf '%s\n' "============================================================"

# ------------------------------------------------------------- the provider
if ! curl -sf -o /dev/null "$KC/realms/$REALM/.well-known/openid-configuration"; then
  echo "Keycloak is not serving discovery at $KC/realms/$REALM." >&2
  echo "Start it with: docker compose --profile auth up -d keycloak" >&2
  exit 2
fi
pass "the provider serves OIDC discovery"

JWKS=$(curl -s "$KC/realms/$REALM/.well-known/openid-configuration" \
       | python -c 'import sys,json;print(json.load(sys.stdin).get("jwks_uri",""))')
if [ -n "$JWKS" ]; then
  pass "discovery advertises a jwks_uri"
else
  fail "discovery advertises a jwks_uri" "absent"
fi

# --------------------------------------------------------- the API's posture
MODE=$(docker compose exec -T api python -c \
  'from memory_platform import auth; print("oauth" if auth.enabled() else "dev-binding")' \
  2>/dev/null | tr -d '\r')
if [ "$MODE" = "oauth" ]; then
  pass "the API is running with OAuth enabled"
else
  echo "  The API is in $MODE mode, so this script would prove nothing." >&2
  echo "  Re-run it with the API started under:" >&2
  echo "    MEMORY_OAUTH_ISSUER=$KC/realms/$REALM \\" >&2
  echo "    MEMORY_OAUTH_AUDIENCE=memory-platform \\" >&2
  echo "    MEMORY_DEV_TENANT_ID= MEMORY_DEV_PROJECT_ID= MEMORY_DEV_PRINCIPAL_ID= \\" >&2
  echo "    docker compose up -d --force-recreate --no-deps api" >&2
  exit 2
fi

# ------------------------------------------------------------------ tokens
CURATOR=$(mint curator)
MALLORY=$(mint mallory)
NOCLAIMS=$(mint noclaims)
for pair in "curator:$CURATOR" "mallory:$MALLORY" "noclaims:$NOCLAIMS"; do
  name=${pair%%:*}; tok=${pair#*:}
  if [ -n "$tok" ]; then pass "$name obtained a token"
  else fail "$name obtained a token" "empty — check required actions on the user"; fi
done

# The claims an actual provider emits, not the ones a test would have invented.
CLAIMS=$(printf '%s' "$CURATOR" | python -c '
import sys, base64, json
p = sys.stdin.read().split(".")[1]
p += "=" * (-len(p) % 4)
c = json.loads(base64.urlsafe_b64decode(p))
print(json.dumps({k: c.get(k) for k in ("iss", "aud", "sub", "org", "project")}))')
printf '        %s\n' "$CLAIMS"
case "$CLAIMS" in
  *'"org":'*'"project":'*) pass "the token carries the org and project claims" ;;
  *) fail "the token carries the org and project claims" "$CLAIMS" ;;
esac

# ------------------------------------------------------------- the decisions
expect "a bound token reads its own project" 200 "$(call "$CURATOR" "$TENANT" "$PROJECT")"

# THE ONE THAT MATTERS. A valid, correctly signed token naming a project that
# does not exist must be refused — and refused in words identical to "you were
# not granted this", or a 403 becomes a directory of every project on the server.
expect "a token naming an unknown project is refused" 403 \
  "$(call "$MALLORY" "$TENANT" "$PROJECT")"
expect "a token with no binding claims is refused" 403 \
  "$(call "$NOCLAIMS" "$TENANT" "$PROJECT")"

UNKNOWN=$(curl -s -H "Authorization: Bearer $MALLORY" \
  "$API/v1/search?q=x&tenant_id=$TENANT&project_id=$PROJECT" | head -c 200)
case "$UNKNOWN" in
  *"does not grant access to a known project"*)
    pass "the refusal does not reveal whether the project exists" ;;
  *) fail "the refusal does not reveal whether the project exists" "$UNKNOWN" ;;
esac

# Scope comes from the TOKEN. Query parameters naming someone else's tenant are
# refused rather than honoured — ADR-0004's whole point.
expect "query parameters cannot override the token's scope" 403 \
  "$(call "$CURATOR" "00000000-0000-0000-0000-000000000001" "$PROJECT")"

expect "no token is unauthorized" 401 \
  "$(curl -s -o /dev/null -w '%{http_code}' \
     "$API/v1/search?q=x&tenant_id=$TENANT&project_id=$PROJECT")"
expect "a malformed token is unauthorized" 401 \
  "$(call "not.a.jwt" "$TENANT" "$PROJECT")"

# ------------------------------------------------- the console's login flow
#
# The browser flow is authorization-code + PKCE and lives in console-app.tsx. It
# was fully implemented and UNREACHABLE: MEMORY_CONSOLE_OIDC_* was never passed
# to the API that serves /v1/console/config, so the login screen could not be
# configured in any deployment and nothing failed loudly to say so.
#
# What is checkable from here is that the API advertises a usable client
# configuration. Completing the redirect needs a browser, so this stops at the
# boundary rather than pretending otherwise.
CONSOLE_CFG=$(curl -s "$API/v1/console/config" 2>/dev/null || echo '{}')
CONFIGURED=$(printf '%s' "$CONSOLE_CFG" | python -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("unreadable"); raise SystemExit
o = d.get("oidc") or {}
print("yes" if o.get("configured") and o.get("authorization_endpoint")
      and o.get("client_id") else "no")' 2>/dev/null || echo "unreadable")

case "$CONFIGURED" in
  yes) pass "the console advertises a usable OIDC client" ;;
  no)  printf '  SKIP  the console OIDC client is not configured\n'
       printf '        set MEMORY_CONSOLE_OIDC_CLIENT_ID / _AUTHORIZATION_ENDPOINT /\n'
       printf '        _TOKEN_ENDPOINT to exercise the browser login flow:\n'
       printf '          MEMORY_CONSOLE_OIDC_CLIENT_ID=%s\n' "$CLIENT"
       printf '          MEMORY_CONSOLE_OIDC_AUTHORIZATION_ENDPOINT=%s/realms/%s/protocol/openid-connect/auth\n' "$KC" "$REALM"
       printf '          MEMORY_CONSOLE_OIDC_TOKEN_ENDPOINT=%s\n' "$TOKEN_URL" ;;
  *)   fail "the console config endpoint is readable" "$CONSOLE_CFG" ;;
esac

printf '%s\n' "------------------------------------------------------------"
if [ "$FAILED" -ne 0 ]; then
  printf 'OAuth verification FAILED\n'; exit 1
fi
printf 'OAuth verification passed\n'
printf 'The browser authorization-code + PKCE flow is a separate concern: this\n'
printf 'exercises token validation and scope resolution, which is what the server\n'
printf 'is responsible for.\n'
