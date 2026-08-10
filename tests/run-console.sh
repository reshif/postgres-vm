#!/usr/bin/env sh
# =============================================================================
# tests/run-console.sh — drive the Knowledge Console in a real browser.
#
#   docker compose --profile console up -d --build console
#   sh tests/run-console.sh
#
# Runs Playwright from its own image, on the compose network, so the console is
# reachable by service name and no browser or Node toolchain has to exist on the
# host. Nothing is installed outside the container.
#
# Separate from run-all.sh on purpose: the browser image is a large pull, and
# the console is profile-gated, so a contributor working on retrieval should not
# have to download Chromium to run the test suite.
# =============================================================================
set -eu

cd "$(dirname "$0")/.."

# Docker Desktop is exposed as `docker.exe` in a WSL shell when the distro has
# not enabled Docker integration. Git Bash and native Linux provide `docker`.
if command -v docker >/dev/null 2>&1 && docker version >/dev/null 2>&1; then
  DOCKER=docker
elif command -v docker.exe >/dev/null 2>&1 && docker.exe version >/dev/null 2>&1; then
  DOCKER=docker.exe
else
  echo "Docker CLI is not available" >&2
  exit 1
fi

# Git Bash on Windows rewrites anything that looks like a Unix path into a
# Windows one before the argument reaches docker, which turns the container-side
# `-w /tests` into `C:/.../tests` and fails with "needs to be an absolute path".
# Turning the rewriting off and using `//tests` (which MSYS leaves alone) makes
# one script work on both platforms.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) HOST_TESTS="$(pwd -W)/tests" ;;
  Linux*)               if [ "$DOCKER" = "docker.exe" ]; then
                           HOST_TESTS="$(wslpath -w "$(pwd)/tests")"
                         else
                           HOST_TESTS="$(pwd)/tests"
                         fi ;;
  *)                    HOST_TESTS="$(pwd)/tests" ;;
esac

NET="$($DOCKER compose ps --format '{{.Name}}' api | head -1)"
if [ -z "$NET" ]; then
  echo "api is not running — start the stack first: docker compose up -d" >&2
  exit 1
fi
if [ -z "$($DOCKER compose ps --format '{{.Name}}' console | head -1)" ]; then
  echo "console is not running — docker compose --profile console up -d --build console" >&2
  exit 1
fi

NETWORK="$($DOCKER inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$NET")"

# The playwright image ships the BROWSERS but not the `playwright` npm package,
# so the client library is installed into /tmp at run time. The browsers already
# present under /ms-playwright are reused — PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD
# stops npm from pulling several hundred megabytes of Chromium it already has.
# A named volume caches the install so a rerun is seconds, not a fresh download.
$DOCKER volume create memory-console-pw >/dev/null

$DOCKER run --rm \
  --network "$NETWORK" \
  -v "${HOST_TESTS}://tests:ro" \
  -v memory-console-pw://pw \
  -e CONSOLE_URL="${CONSOLE_URL:-http://console:3000}" \
  -e PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
  -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
  -w //tests \
  mcr.microsoft.com/playwright:v1.50.0-noble \
  sh -c 'test -d /pw/node_modules/playwright || npm i --silent --prefix /pw playwright@1.50.0 >/dev/null 2>&1;
         export NODE_PATH=/pw/node_modules;
         node //tests/test_console.js'
