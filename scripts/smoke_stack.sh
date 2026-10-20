#!/usr/bin/env bash
# Functional smoke test of the production stack (docker-compose.prod.yml).
#
#   scripts/smoke_stack.sh                       # build, seed the fixture, probe, tear down
#   scripts/smoke_stack.sh --emulation           # the same under free-tier CPU/RAM limits
#   scripts/smoke_stack.sh --integration-via-pgbouncer [--prepared-statements N]
#                                                # pytest tests/integration through PgBouncer
#
# Default cycle: build the API image (ATUS_BUILD_SHA = the checkout's commit),
# start db -> seed (fixture dataset) -> api, wait for the API's health check,
# run deploy/stack/smoke_probe.py from inside the stack network (the API port
# is never published), print the result, and remove the stack and its volume.
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
#   --keep                    leave the stack running (prints how to remove it)
#   --python PATH             interpreter for --integration-via-pgbouncer
#                             (default .venv/bin/python, then python3)
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
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

secret() { od -An -tx1 -N24 /dev/urandom | tr -d ' \n'; }
# Mask the password in any connection URL a failing test or log line prints
# (pytest shows reprs of settings objects that carry the database URL).
redact() { sed -E 's#(://[^:/@[:space:]]+:)[^@[:space:]]+@#\1***@#g'; }

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

cleanup() {
  status=$?
  if [ "$status" != 0 ]; then
    echo "--- stack logs (last 80 lines per service) ---"
    compose --profile pgbouncer logs --no-color --tail 80 2>&1 | redact || true
  fi
  if [ "$keep" = 1 ]; then
    echo "stack kept: docker compose -p $project ${files[*]} down -v  (env file: $env_file)"
  else
    compose --profile pgbouncer --profile smoke down -v --remove-orphans >/dev/null 2>&1 || true
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
  probe_args=(--fixture)
  if [ "$skip_protection" = 1 ]; then
    probe_args+=(--skip-protection-checks)
  else
    probe_args+=(--expect-build "$build_sha")
  fi
  compose --profile smoke run --rm --no-deps smoke "${probe_args[@]}" 2>&1 | redact
else
  echo "== start db and PgBouncer (transaction mode, max_prepared_statements=$prepared)"
  compose --profile pgbouncer up -d --wait --wait-timeout 300 pgbouncer
  port=$(compose --profile pgbouncer port pgbouncer 6432 | sed 's/.*://')
  superuser_password=$(sed -n 's/^ATUS_STACK_PG_SUPERUSER_PASSWORD=//p' "$env_file")
  if [ -z "$python_bin" ]; then
    if [ -x .venv/bin/python ]; then python_bin=.venv/bin/python; else python_bin=python3; fi
  fi
  echo "== pytest tests/integration through PgBouncer on 127.0.0.1:$port"
  ATUS_TEST_DATABASE_URL="postgresql://postgres:${superuser_password}@127.0.0.1:${port}/atus_test_pgbouncer" \
    "$python_bin" -m pytest tests/integration -q -p no:cacheprovider 2>&1 | redact
fi
