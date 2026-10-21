#!/usr/bin/env bash
# Functional smoke test of the production stack (docker-compose.prod.yml).
#
#   scripts/smoke_stack.sh                       # build, seed the fixture, probe, tear down
#   scripts/smoke_stack.sh --emulation           # the same under free-tier CPU/RAM limits
#   scripts/smoke_stack.sh --edge [--e2e]        # ... and the public origin: the Worker,
#                                                # the SPA and the static snapshot
#   scripts/smoke_stack.sh --integration-via-pgbouncer [--prepared-statements N]
#                                                # pytest tests/integration through PgBouncer
#
# Default cycle: build the API image (ATUS_BUILD_SHA = the checkout's commit),
# start db -> seed (a serving database built from the fixture dataset) ->
# roles (scripts/ops/roles.sql) -> api, wait for the API's health check, run
# deploy/stack/smoke_probe.py from inside the stack network (the API port is
# never published), print the result, and remove the stack and its volume.
#
# --edge then does what a deploy does, in the same order (ADR-011 "Deploy"):
#   1. binds the API to its data (ATUS_EXPECTED_DATA_VERSION = the seed's
#      version, as the release switch sets it on Render; the API restarts);
#   2. builds the SPA (frontend/: npm run build, same-origin) unless --assets;
#   3. builds the static snapshot into the assets with scripts/build_snapshot.py
#      and the SPA's request generator, against the `snapshot-api` service
#      (the same image and database, generous per-client limits, loopback);
#      --skip-refused, because the small fixture cannot answer every
#      prewarmed spec;
#   4. starts the Worker (`edge`, wrangler dev in local mode) with the release
#      vars DATA_VERSION, ANALYTICS_VERSION and SNAPSHOT_ID, on 127.0.0.1;
#   5. runs deploy/stack/edge_probe.py from the host against it, then
#      deploy.yml's own post-deploy gate, scripts/ops/deploy_smoke.py, with
#      the deploy's flags (the snapshot-api service as the origin for the
#      origin lock; the §9 comparison needs this checkout's package in
#      --python), and with --e2e the Playwright suite through it
#      (E2E_BASE_URL; projects from E2E_PROJECTS, default "desktop mobile
#      webkit") and deploy.yml's browser smoke
#      (playwright.deployed.config.ts, PUBLIC_BASE_URL).
#
# --integration-via-pgbouncer starts only db + PgBouncer (transaction mode,
# emulation profile) and runs the existing integration suite from this
# checkout's virtualenv through it, as the bootstrap superuser (the suite
# creates its own database). --prepared-statements sets PgBouncer's
# max_prepared_statements (default 1000, Neon's value; 0 = no support).
#
# Secrets are random, written to a private temporary env file and deleted on
# exit; nothing prints them. Host ports are random free loopback ports, so the
# smoke never collides with a running stack. Functional only: no timings.
#
# Options:
#   --project NAME            compose project name (default atus-stack-smoke)
#   --emulation               add docker-compose.emulation.yml
#   --no-build                use ATUS_STACK_IMAGE as is (e.g. a registry digest);
#                             set ATUS_BUILD_SHA to the commit it was built from
#   --skip-protection-checks  for API images that predate the Phase 5 API contract
#                             (edge auth, headers, limits; see smoke_probe.py)
#   --edge                    also the edge cycle above (needs node and npm)
#   --e2e                     with --edge: also Playwright through the edge
#   --assets DIR              with --edge: publish this SPA build (a copy of it)
#                             instead of building frontend/
#   --keep                    leave the stack running; prints the exact teardown
#                             (env file, every profile) and its secrets directory
#   --python PATH             interpreter for --integration-via-pgbouncer and the
#                             host-side edge steps (default .venv/bin/python, then python3)
set -euo pipefail

cd "$(dirname "$0")/.."

project=atus-stack-smoke
mode=smoke
emulation=0
build=1
keep=0
skip_protection=0
prepared=1000
python_bin=
edge=0
e2e=0
assets_src=

while [ $# -gt 0 ]; do
  case "$1" in
    --project) project=$2; shift 2 ;;
    --emulation) emulation=1; shift ;;
    --no-build) build=0; shift ;;
    --skip-protection-checks) skip_protection=1; shift ;;
    --keep) keep=1; shift ;;
    --integration-via-pgbouncer) mode=pgbouncer; emulation=1; shift ;;
    --prepared-statements) prepared=$2; shift 2 ;;
    --python) python_bin=$2; shift 2 ;;
    --edge) edge=1; shift ;;
    --e2e) e2e=1; shift ;;
    --assets) assets_src=$2; shift 2 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
if [ "$e2e" = 1 ] && [ "$edge" = 0 ]; then
  echo "--e2e needs --edge" >&2; exit 2
fi
if [ -z "$python_bin" ]; then
  if [ -x .venv/bin/python ]; then python_bin=.venv/bin/python; else python_bin=python3; fi
fi

secret() { od -An -tx1 -N24 /dev/urandom | tr -d ' \n'; }
# Mask the password in any connection URL a failing test or log line prints
# (pytest shows reprs of settings objects that carry the database URL).
redact() { sed -E 's#(://[^:/@[:space:]]+:)[^@[:space:]]+@#\1***@#g'; }

# The response format the checkout's API sends (atus_pipeline.api.RESPONSE_FORMAT):
# the probe and the snapshot build expect it, so a bump needs no edit here.
response_format=$(sed -n 's/^RESPONSE_FORMAT = "\([0-9][0-9]*\)"$/\1/p' \
  src/atus_pipeline/api/__init__.py)
[ -n "$response_format" ] || {
  echo "no RESPONSE_FORMAT in src/atus_pipeline/api/__init__.py" >&2; exit 2; }

workdir=$(mktemp -d "${TMPDIR:-/tmp}/atus-smoke.XXXXXX")
chmod 700 "$workdir"
env_file="$workdir/stack.env"
build_sha=${ATUS_BUILD_SHA:-$(git rev-parse HEAD 2>/dev/null || echo dev)}
umask 077
cat > "$env_file" <<EOF
ATUS_STACK_PG_SUPERUSER_PASSWORD=$(secret)
ATUS_STACK_OWNER_PASSWORD=$(secret)
ATUS_STACK_API_DB_PASSWORD=$(secret)
ATUS_STACK_EDGE_SECRET=$(secret)
ATUS_STACK_OPS_TOKEN=$(secret)
ATUS_STACK_EDGE_OPS_TOKEN=$(secret)
ATUS_STACK_IP_HMAC_KEY=$(secret)
ATUS_STACK_DB_PORT=
ATUS_STACK_PGBOUNCER_PORT=
ATUS_STACK_EDGE_PORT=
ATUS_BUILD_SHA=$build_sha
ATUS_VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' src/atus_pipeline/__init__.py)
PGBOUNCER_MAX_PREPARED_STATEMENTS=$prepared
EOF
if [ "$build" = 0 ]; then
  echo "ATUS_STACK_PULL_POLICY=missing" >> "$env_file"
fi
umask 022

files=(-f docker-compose.prod.yml)
if [ "$emulation" = 1 ]; then
  files+=(-f docker-compose.emulation.yml)
fi
compose() { docker compose -p "$project" --env-file "$env_file" "${files[@]}" "$@"; }
# Every profile, so an --edge stack's Worker goes too; the env file carries the
# secrets compose needs to read docker-compose.prod.yml at all.
teardown_args=(--profile pgbouncer --profile smoke --profile snapshot --profile edge
               down -v --remove-orphans)

cleanup() {
  status=$?
  if [ "$status" != 0 ]; then
    echo "--- stack logs (last 80 lines per service) ---"
    compose --profile pgbouncer --profile snapshot --profile edge logs --no-color --tail 80 \
      2>&1 | redact || true
  fi
  if [ "$keep" = 1 ]; then
    echo "stack kept. To remove it, from $PWD, then delete its private work directory" \
      "(it holds the stack's secrets):"
    printf '  docker compose -p %q --env-file %q' "$project" "$env_file"
    printf ' %q' "${files[@]}" "${teardown_args[@]}"
    printf '\n  rm -rf %q\n' "$workdir"
  else
    if ! teardown=$(compose "${teardown_args[@]}" 2>&1); then
      echo "teardown failed:" >&2
      echo "$teardown" | redact >&2
    fi
    rm -rf "$workdir"
  fi
  exit "$status"
}
trap cleanup EXIT

if [ "$mode" = smoke ]; then
  if [ "$build" = 1 ]; then
    echo "== build the API image ($build_sha)"
    compose build api
  fi
  echo "== start db, seed (fixture dataset) and api"
  up_args=(-d --wait --wait-timeout 300)
  if [ "$build" = 0 ]; then
    up_args+=(--no-build)
  fi
  compose up "${up_args[@]}" api
  compose logs --no-color seed | grep -E 'seed: ' || true

  echo "== smoke probe (inside the stack network)"
  probe_args=(--fixture --response-format "$response_format")
  if [ "$skip_protection" = 1 ]; then
    probe_args+=(--skip-protection-checks)
  else
    probe_args+=(--expect-build "$build_sha")
  fi
  compose --profile smoke run --rm --no-deps smoke "${probe_args[@]}" 2>&1 | redact

  if [ "$edge" = 1 ]; then
    setting() { sed -n "s/^$1=//p" "$env_file" | tail -n 1; }
    version=$(compose logs --no-color seed | sed -n 's/.*seed: ready; data_version=//p' \
      | tail -n 1 | tr -d '\r')
    [ -n "$version" ] || { echo "no data_version in the seed's log" >&2; exit 1; }

    echo "== bind the API to $version (as the release switch does on Render)"
    echo "ATUS_EXPECTED_DATA_VERSION=$version" >> "$env_file"
    compose up -d --wait --wait-timeout 300 --no-build --no-deps api

    assets="$workdir/assets"
    if [ -n "$assets_src" ]; then
      echo "== publish the SPA build in $assets_src"
      cp -R "$assets_src" "$assets"
    else
      echo "== build the SPA (same origin, production build)"
      (cd frontend && VITE_API_BASE_URL='' npm run build --silent)
      cp -R frontend/dist "$assets"
    fi
    rm -rf "$assets/snap"

    echo "== build the static snapshot (snapshot-api: the same image and data)"
    compose --profile snapshot up -d --wait --wait-timeout 300 --no-build --no-deps snapshot-api
    direct_api="http://127.0.0.1:$(compose --profile snapshot port snapshot-api 10000 \
      | sed 's/.*://')"
    snapshot_id=$(EDGE_AUTH_SECRET=$(setting ATUS_STACK_EDGE_SECRET) "$python_bin" \
      scripts/build_snapshot.py --api-base "$direct_api" --out "$assets/snap" \
      --requests-script frontend/scripts/snapshot-requests.mjs --api-build "$build_sha" \
      --data-version "$version" --response-format "$response_format" --skip-refused)
    pointer() {
      "$python_bin" -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' \
        "$assets/snap/current.json" "$1"
    }
    {
      echo "ATUS_STACK_ASSETS_DIR=$assets"
      echo "ATUS_STACK_SNAPSHOT_ID=$snapshot_id"
      echo "ATUS_STACK_ANALYTICS_VERSION=$(pointer analytics_version)"
      echo "ATUS_STACK_RESPONSE_FORMAT=$(pointer response_format)"
    } >> "$env_file"

    echo "== start the Worker (wrangler dev, local mode) with the release vars"
    if [ "$build" = 1 ]; then
      compose --profile edge build edge
    fi
    compose --profile edge up -d --wait --wait-timeout 300 --no-deps edge
    edge_base="http://127.0.0.1:$(compose --profile edge port edge 8787 | sed 's/.*://')"
    echo "   edge: $edge_base  snapshot: $snapshot_id"

    echo "== edge probe (from the host, through the Worker)"
    ATUS_PROBE_EDGE_OPS_TOKEN=$(setting ATUS_STACK_EDGE_OPS_TOKEN) \
      ATUS_PROBE_DATA_VERSION=$version \
      "$python_bin" deploy/stack/edge_probe.py --base "$edge_base" --assets "$assets" \
      --direct-api "$direct_api"

    echo "== post-deploy smoke, as deploy.yml runs it (snapshot-api stands in for the origin)"
    smoke_args=(--base-url "$edge_base" --expect-data-version "$version"
                --expect-snapshot-id "$snapshot_id" --require-snapshot --recompute 2
                --max-requests 30 --origin-lock --ops-budget)
    if [ "$skip_protection" = 0 ]; then
      smoke_args+=(--expect-build "$build_sha")
    fi
    RENDER_ORIGIN=$direct_api EDGE_OPS_TOKEN=$(setting ATUS_STACK_EDGE_OPS_TOKEN) \
      "$python_bin" scripts/ops/deploy_smoke.py "${smoke_args[@]}"
    compose --profile snapshot rm -fsv snapshot-api >/dev/null 2>&1 || true

    if [ "$e2e" = 1 ]; then
      echo "== Playwright through the edge (stack mode: $edge_base)"
      playwright_projects=()
      for e2e_project in ${E2E_PROJECTS:-desktop mobile webkit}; do
        playwright_projects+=(--project "$e2e_project")
      done
      (cd frontend && E2E_BASE_URL="$edge_base" npx playwright test "${playwright_projects[@]}")

      echo "== deployed browser smoke, as deploy.yml runs it ($edge_base)"
      (cd frontend && PUBLIC_BASE_URL="$edge_base" \
        npx playwright test -c playwright.deployed.config.ts)
    fi
    if [ "$keep" = 1 ]; then
      echo "edge kept at $edge_base"
    fi
  fi
else
  echo "== start db and PgBouncer (transaction mode, max_prepared_statements=$prepared)"
  compose --profile pgbouncer up -d --wait --wait-timeout 300 pgbouncer
  port=$(compose --profile pgbouncer port pgbouncer 6432 | sed 's/.*://')
  superuser_password=$(sed -n 's/^ATUS_STACK_PG_SUPERUSER_PASSWORD=//p' "$env_file")
  echo "== pytest tests/integration through PgBouncer on 127.0.0.1:$port"
  ATUS_TEST_DATABASE_URL="postgresql://postgres:${superuser_password}@127.0.0.1:${port}/atus_test_pgbouncer" \
    "$python_bin" -m pytest tests/integration -q -p no:cacheprovider 2>&1 | redact
fi
