#!/usr/bin/env bash
# The Playwright E2E suite with its browsers in the official Playwright Linux
# container — for machines where a browser cannot launch natively (Firefox
# exits at launch on macOS 27; brief §14, §17 cross-browser).
#
#   scripts/e2e_in_container.sh --project firefox   # Firefox (the @core journeys)
#   scripts/e2e_in_container.sh                     # every project
#   scripts/e2e_in_container.sh --print-image       # the image it would use, then exit
#
# Every argument except --print-image goes to `npx playwright test`.
#
# How: the container (mcr.microsoft.com/playwright:v<version>-noble, where
# <version> is @playwright/test in frontend/package-lock.json) runs only
# `playwright run-server`, published on 127.0.0.1. The test runner, `vite
# preview` and scripts/e2e_backend.py stay on this machine exactly as in `npm
# run test:e2e`; E2E_BROWSER_SERVER makes playwright.config.ts connect every
# project to the container (e2e/browserServer.ts) with exposeNetwork
# '<loopback>', so the browsers' requests for localhost come back through the
# test runner: the app is served from the same origin as in every other run,
# with no host.docker.internal and no extra published port.
#
# The server is the lockfile's own Playwright (frontend/node_modules, mounted
# read-only, pure JavaScript), so client, server and the image's browser builds
# are one version; the script refuses a node_modules that differs from the
# lockfile (run `npm ci` in frontend/ first) and an image of another version.
# The container is removed on exit, whatever the result; a container another
# run started under the same name is never touched.
#
# Environment:
#   E2E_BROWSER_SERVER_PORT  host port of the browser server, on 127.0.0.1
#                            (default 8139)
#   E2E_BROWSER_CONTAINER    container name (default atus-e2e-browsers); the
#                            script refuses to start if it already exists
#   E2E_PLAYWRIGHT_IMAGE     another reference to the same image, e.g. pinned
#                            by digest (mcr.microsoft.com/playwright:v<version>-noble@sha256:...)
#   The usual E2E variables pass through to Playwright (playwright.config.ts):
#   E2E_FRONTEND_PORT, E2E_BACKEND_PORT, ATUS_E2E_DATABASE_URL,
#   E2E_BACKEND_PYTHON, E2E_BASE_URL.
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd)
frontend="$repo_root/frontend"

die() {
  echo "e2e_in_container: $*" >&2
  exit 2
}

print_image=0
if [ "${1:-}" = "--print-image" ]; then
  [ $# -eq 1 ] || die "--print-image takes no other arguments"
  print_image=1
fi

command -v node >/dev/null 2>&1 || die "node is required (it runs the Playwright test runner)"

# The one version everything must be: @playwright/test as the lockfile pins it.
version=$(node -e '
  const lock = require(process.argv[1])
  const entry = (lock.packages || {})["node_modules/@playwright/test"]
  if (!entry || !entry.version) process.exit(1)
  console.log(entry.version)
' "$frontend/package-lock.json") || die "cannot read @playwright/test from frontend/package-lock.json"
case "$version" in
  [0-9]*.[0-9]*.[0-9]*) ;;
  *) die "unexpected @playwright/test version '$version' in frontend/package-lock.json" ;;
esac
image="mcr.microsoft.com/playwright:v${version}-noble"
if [ -n "${E2E_PLAYWRIGHT_IMAGE:-}" ]; then
  case "$E2E_PLAYWRIGHT_IMAGE" in
    "$image" | "$image"@sha256:*) image=$E2E_PLAYWRIGHT_IMAGE ;;
    *) die "E2E_PLAYWRIGHT_IMAGE=$E2E_PLAYWRIGHT_IMAGE is not $image (optionally @sha256:<digest>): the browsers must match the lockfile's Playwright" ;;
  esac
fi

if [ "$print_image" = 1 ]; then
  echo "$image"
  exit 0
fi

# The installed client and the server the container will run must be that version.
for package in @playwright/test playwright playwright-core; do
  manifest="$frontend/node_modules/$package/package.json"
  [ -f "$manifest" ] || die "frontend/node_modules/$package is missing: run \`npm ci\` in frontend/ first"
  installed=$(node -p 'require(process.argv[1]).version' "$manifest")
  [ "$installed" = "$version" ] ||
    die "frontend/node_modules/$package is $installed, the lockfile pins $version: run \`npm ci\` in frontend/"
done

port=${E2E_BROWSER_SERVER_PORT:-8139}
case "$port" in
  '' | *[!0-9]*) die "E2E_BROWSER_SERVER_PORT=$port is not a port number" ;;
esac
[ "$port" -ge 1 ] && [ "$port" -le 65535 ] || die "E2E_BROWSER_SERVER_PORT=$port is not a port number"
container=${E2E_BROWSER_CONTAINER:-atus-e2e-browsers}
case "$container" in
  [a-zA-Z0-9]*) ;;
  *) die "E2E_BROWSER_CONTAINER=$container is not a container name" ;;
esac
case "$container" in
  *[!a-zA-Z0-9_.-]*) die "E2E_BROWSER_CONTAINER=$container is not a container name" ;;
esac

command -v docker >/dev/null 2>&1 || die "docker is required"
if docker container inspect "$container" >/dev/null 2>&1; then
  die "a container named $container already exists (another run, or a leftover: docker rm -f $container), or set E2E_BROWSER_CONTAINER"
fi
if ! docker image inspect "$image" >/dev/null 2>&1; then
  echo "== Pulling $image"
  docker pull "$image"
fi

# This run's mark on its container. The cleanup removes the container under
# $container only while it carries this mark: when `docker run` fails because
# another run took the name after the check above (say, during a long pull),
# that run's container is left alone; one this run created but could not
# start (a port in use) is still removed.
run_label=atus.e2e-browsers.run
run_id="$$.$(date +%s).${RANDOM}"
started=0
cleanup() {
  [ "$started" = 1 ] || return 0
  local owner
  owner=$(docker container inspect --format "{{index .Config.Labels \"$run_label\"}}" \
    "$container" 2>/dev/null) || return 0
  if [ "$owner" = "$run_id" ]; then
    docker rm -f "$container" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "== Browser server: $image (Playwright $version) as $container on 127.0.0.1:$port"
# --init reaps the browsers' processes; --ipc=host gives Chromium enough
# shared memory; pwuser is the image's unprivileged user. Published on
# loopback only: a browser server drives browsers for whoever connects.
started=1
docker run --detach --rm --init --ipc=host --user pwuser --workdir /home/pwuser \
  --name "$container" --label "$run_label=$run_id" \
  --publish "127.0.0.1:${port}:3000" \
  --volume "$frontend/node_modules:/opt/atus-e2e/node_modules:ro" \
  "$image" \
  node /opt/atus-e2e/node_modules/playwright-core/cli.js run-server --port 3000 --host 0.0.0.0 \
  >/dev/null

ready=0
for _ in $(seq 1 60); do
  # Not `grep -q`: it stops reading at the first match, and under pipefail
  # the SIGPIPE of a docker logs still writing would fail the check.
  if docker logs "$container" 2>&1 | grep 'Listening on ws://' >/dev/null; then
    ready=1
    break
  fi
  if [ "$(docker container inspect --format '{{.State.Running}}' "$container" 2>/dev/null)" != "true" ]; then
    break
  fi
  sleep 1
done
if [ "$ready" != 1 ]; then
  echo "e2e_in_container: the browser server did not start; its log:" >&2
  docker logs "$container" >&2 2>&1 || true
  exit 1
fi

echo "== Playwright on this machine, browsers in the container: npx playwright test $*"
status=0
(
  cd "$frontend"
  E2E_BROWSER_SERVER="ws://127.0.0.1:${port}/" exec npx --no-install playwright test "$@"
) || status=$?
exit "$status"
