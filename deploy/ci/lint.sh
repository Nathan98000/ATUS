#!/usr/bin/env bash
# Lint the packaging files with tools that are already installed; never downloads one.
#
#   deploy/ci/lint.sh [ROOT]      # ROOT: the checkout to lint (default: this one)
#
# Always run (the Docker CLI is the only requirement):
#   dockerfile  `docker build --check` on every Dockerfile, with the build context
#               it is built from: BuildKit's built-in rules (instruction casing,
#               JSON-form CMD/ENTRYPOINT, undefined ARGs, secrets in ARG/ENV,
#               deprecated instructions, ...). Any warning fails. Needs a daemon,
#               and reads base-image metadata from the registry or local cache.
#   compose     `docker compose config --quiet` on every compose file and the
#               combinations CI uses (dev; prod with every profile; prod +
#               emulation with PgBouncer), with throwaway values for the
#               required secrets. Never contacts the daemon.
#   shell       `sh -n` / `bash -n` (per shebang) on every shell script under
#               deploy/, scripts/ and requirements/: syntax only.
# Run only when already on PATH (reported as SKIP otherwise, never installed):
#   hadolint    Dockerfile best practices (errors fail; warnings are printed)
#   ShellCheck  shell script analysis (warnings and errors fail); the
#               GitHub-hosted Ubuntu runners have it, so CI always runs it
#
# Prints one line per file and check; exits 1 if anything failed. Python files
# are linted by `ruff check .`, which CI already runs.
set -uo pipefail

cd "${1:-$(dirname "$0")/../..}" || exit 2

failures=0
pass() { echo "[PASS] $1"; }
fail() { echo "[FAIL] $1"; failures=$((failures + 1)); }
skip() { echo "[SKIP] $1"; }

# Every Dockerfile in the repository and the build context it is built with
# (docker-compose.*.yml and the CI image job). tests/deploy/test_ci_lint.py
# fails if a Dockerfile is missing here.
dockerfiles=(
  "Dockerfile|."
  "deploy/pgbouncer/Dockerfile|deploy/pgbouncer"
  "deploy/edge/Dockerfile|."
)

# Throwaway values for the variables docker-compose.prod.yml requires; config
# only interpolates them, nothing starts.
stack_env=(
  ATUS_STACK_PG_SUPERUSER_PASSWORD=lint ATUS_STACK_OWNER_PASSWORD=lint
  ATUS_STACK_API_DB_PASSWORD=lint ATUS_STACK_EDGE_SECRET=lint ATUS_STACK_OPS_TOKEN=lint
)
compose_sets=(
  "-f docker-compose.yml"
  "-f docker-compose.prod.yml --profile dump --profile snapshot --profile edge --profile smoke"
  "-f docker-compose.prod.yml -f docker-compose.emulation.yml --profile pgbouncer"
)

if ! command -v docker >/dev/null 2>&1; then
  fail "docker: the Docker CLI is required for the Dockerfile and compose checks"
else
  for entry in "${dockerfiles[@]}"; do
    file=${entry%%|*}
    context=${entry#*|}
    if output=$(docker build --check --quiet -f "$file" "$context" 2>&1); then
      pass "dockerfile  $file"
    else
      fail "dockerfile  $file"
      printf '%s\n' "$output" | sed 's/^/    /'
    fi
  done

  for set in "${compose_sets[@]}"; do
    # Only the throwaway values (plus what the CLI needs) reach interpolation,
    # so a developer's shell or .env cannot change the result.
    # shellcheck disable=SC2086  # $set is a list of options
    if output=$(env -i PATH="$PATH" HOME="$HOME" ${DOCKER_HOST:+DOCKER_HOST="$DOCKER_HOST"} \
        "${stack_env[@]}" docker compose $set config --quiet 2>&1); then
      pass "compose     $set"
    else
      fail "compose     $set"
      printf '%s\n' "$output" | sed 's/^/    /'
    fi
  done
fi

while IFS= read -r script; do
  case $(head -n 1 "$script") in
    *bash*) shell="bash" ;;
    *) shell="sh" ;;
  esac
  if output=$("$shell" -n "$script" 2>&1); then
    pass "shell       $script ($shell -n)"
  else
    fail "shell       $script ($shell -n)"
    printf '%s\n' "$output" | sed 's/^/    /'
  fi
done < <(find deploy scripts requirements -name '*.sh' -type f | sort)

if command -v hadolint >/dev/null 2>&1; then
  for entry in "${dockerfiles[@]}"; do
    file=${entry%%|*}
    if hadolint --failure-threshold error "$file"; then
      pass "hadolint    $file"
    else
      fail "hadolint    $file"
    fi
  done
else
  skip "hadolint    not installed (this script installs nothing)"
fi

if command -v shellcheck >/dev/null 2>&1; then
  while IFS= read -r script; do
    if shellcheck --severity=warning "$script"; then
      pass "shellcheck  $script"
    else
      fail "shellcheck  $script"
    fi
  done < <(find deploy scripts requirements -name '*.sh' -type f | sort)
else
  skip "shellcheck  not installed (this script installs nothing)"
fi

echo "lint: $failures failed"
[ "$failures" -eq 0 ]
