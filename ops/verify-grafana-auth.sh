#!/usr/bin/env sh
# =============================================================================
# ops/verify-grafana-auth.sh — check the production Grafana auth posture.
#
#   sh ops/verify-grafana-auth.sh                    against a running prod stack
#   GRAFANA_URL=https://grafana.example.com sh ops/verify-grafana-auth.sh
#
# WHAT THIS CAN AND CANNOT TELL YOU.
#
# It cannot complete an OAuth flow. That needs a real identity provider, a
# registered redirect URI and a real account, and no script here can stand in for
# it. What it CAN do is catch the failures that are invisible until someone tries
# to log in — or worse, until someone who should not have logged in does:
#
#   * anonymous access still on, so the dashboards are public
#   * the username/password form still served, so SSO is an additional door
#     rather than the only one
#   * the built-in admin still on the default password
#   * OAuth "enabled" but with no endpoints, so the button goes nowhere
#   * no domain or group restriction, so every account the IdP will authenticate
#     gets a Grafana account
#
# The last one is the reason this script exists. It is the only item on the list
# that fails SILENTLY and PERMISSIVELY: everything works, people log in, and the
# deployment is wide open to anyone the provider recognises.
# =============================================================================
set -eu

GRAFANA_URL="${GRAFANA_URL:-http://localhost:3001}"
FAILED=0

pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; FAILED=1; }
warn() { printf '  WARN  %s\n' "$1"; }

req() {
  # -s silent, -S show errors, -k tolerate self-signed in staging.
  curl -sSk --max-time 10 "$@"
}

printf 'Grafana auth posture: %s\n' "$GRAFANA_URL"
printf '%s\n' "============================================================"

# ---------------------------------------------------------------- reachable
if ! req -o /dev/null "$GRAFANA_URL/api/health"; then
  printf 'Grafana is not reachable at %s\n' "$GRAFANA_URL" >&2
  exit 2
fi
pass "Grafana is reachable"

# ------------------------------------------------------------- anonymous off
# /api/search needs an authenticated session. With anonymous access on it
# returns 200 and a dashboard list to a caller carrying no credentials at all.
anon_status="$(req -o /dev/null -w '%{http_code}' "$GRAFANA_URL/api/search?limit=1")"
case "$anon_status" in
  401|403) pass "anonymous access is refused (HTTP $anon_status)" ;;
  200)     fail "ANONYMOUS ACCESS IS ON — dashboards are readable with no login" ;;
  *)       warn "unexpected status from /api/search: $anon_status" ;;
esac

# --------------------------------------------------- settings, as Grafana sees
# Read from the LOGIN PAGE, not /api/frontend/settings.
#
# That endpoint looks like the obvious source and is the wrong one: on Grafana 12
# with anonymous access off it answers an unauthenticated caller with an almost
# empty document, so a check written against it reports "no OAuth provider
# configured" on a correctly configured server. Measured, not assumed — it
# returned no oauth key at all on a server whose OAuth works.
#
# The login page is served unauthenticated because it has to be, and it carries
# the bootstrap settings the page itself renders from.
login_page="$(req "$GRAFANA_URL/login" || echo '')"

has() { printf '%s' "$login_page" | grep -q "$1"; }

if has 'disableLoginForm":true'; then
  pass "the username/password login form is disabled"
elif has 'disableLoginForm":false'; then
  fail "THE LOGIN FORM IS STILL SERVED — the built-in admin is reachable beside SSO"
else
  warn "could not determine whether the login form is disabled"
fi

# The strongest available check short of a real IdP: follow the provider's own
# entry point and read the redirect Grafana builds. An "enabled" provider with no
# authorization endpoint produces a button that goes nowhere, and Grafana does
# not treat that as a configuration error — this catches it.
redirect="$(req -o /dev/null -w '%{redirect_url}' "$GRAFANA_URL/login/generic_oauth" || echo '')"
if [ -z "$redirect" ]; then
  fail "NO OAUTH PROVIDER IS CONFIGURED, or it has no authorization endpoint"
else
  pass "the OAuth entry point redirects to the identity provider"
  printf '        %s\n' "$(printf '%s' "$redirect" | cut -c1-96)..."

  case "$redirect" in
    *code_challenge_method=S256*) pass "PKCE is in use (S256)" ;;
    *) fail "no PKCE challenge in the authorization request" ;;
  esac
  case "$redirect" in
    *state=*) pass "a state parameter is present (CSRF defence)" ;;
    *) fail "no state parameter in the authorization request" ;;
  esac
  # A redirect_uri built from the wrong root_url is the classic symptom of
  # Grafana behind a proxy: the flow starts and fails on the way back.
  case "$redirect" in
    *redirect_uri=https%3A%2F%2F*) pass "the redirect URI is https" ;;
    *redirect_uri=http%3A%2F%2Flocalhost*)
      warn "the redirect URI points at localhost — correct only for a local test" ;;
    *) fail "the redirect URI is not https; check GF_SERVER_ROOT_URL" ;;
  esac
fi

# ------------------------------------------------- the access decision itself
# Authentication is not authorisation. Grafana does not expose the allow-list
# over the API, so this is checked from the environment of the running
# container — which is where the answer actually lives.
if command -v docker >/dev/null 2>&1; then DOCKER=docker
elif command -v docker.exe >/dev/null 2>&1; then DOCKER=docker.exe
else DOCKER=""; fi

if [ -n "$DOCKER" ] && $DOCKER compose ps grafana >/dev/null 2>&1; then
  env_dump="$($DOCKER compose exec -T grafana env 2>/dev/null || echo '')"
  domains="$(printf '%s' "$env_dump" | sed -n 's/^GF_AUTH_GENERIC_OAUTH_ALLOWED_DOMAINS=//p')"
  groups="$(printf '%s' "$env_dump" | sed -n 's/^GF_AUTH_GENERIC_OAUTH_ALLOWED_GROUPS=//p')"
  signup="$(printf '%s' "$env_dump" | sed -n 's/^GF_AUTH_GENERIC_OAUTH_ALLOW_SIGN_UP=//p')"
  admin_pw="$(printf '%s' "$env_dump" | sed -n 's/^GF_SECURITY_ADMIN_PASSWORD=//p')"
  assign_admin="$(printf '%s' "$env_dump" | sed -n 's/^GF_AUTH_GENERIC_OAUTH_ALLOW_ASSIGN_GRAFANA_ADMIN=//p')"

  if [ -n "$domains" ] || [ -n "$groups" ]; then
    pass "access is restricted (domains='$domains' groups='$groups')"
  elif [ "$signup" = "false" ]; then
    pass "sign-up is disabled; only pre-provisioned accounts can log in"
  else
    fail "NO DOMAIN OR GROUP RESTRICTION AND SIGN-UP IS ON — every account the identity provider will authenticate gets a Grafana account"
  fi

  case "$admin_pw" in
    ""|"admin") fail "the built-in admin password is unset or still 'admin'" ;;
    *)          pass "the built-in admin password has been changed" ;;
  esac

  if [ "$assign_admin" = "true" ]; then
    fail "an IdP claim can grant server-wide Grafana Admin"
  else
    pass "server-wide Grafana Admin cannot be granted by a claim"
  fi
else
  warn "docker compose unavailable; skipped the allow-list and admin checks"
  warn "these are the checks that fail permissively — run this on the host"
fi

printf '%s\n' "------------------------------------------------------------"
if [ "$FAILED" -ne 0 ]; then
  printf 'Grafana auth posture: FAILED\n'
  printf 'Do not expose this deployment until the failures above are fixed.\n'
  exit 1
fi
printf 'Grafana auth posture: OK\n'
printf 'The OAuth redirect and token exchange still need a real IdP — this\n'
printf 'script checks posture, not the flow.\n'
