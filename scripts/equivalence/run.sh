#!/usr/bin/env bash
# 3-way HTTP equivalence replay (brief §9). See scripts/equivalence/README.md.
#
#   scripts/equivalence/run.sh setup
#       Prepare the baseline: a detached git worktree of the Phase 1-4 commit
#       (outside this checkout) with its own virtualenv, installed from this
#       checkout's hash-locked requirements/api.lock.txt so both APIs run the
#       same library versions.
#
#   scripts/equivalence/run.sh --specs DIR --out DIR [replay.py options]
#       Prepare the baseline if needed, then run replay.py: it starts the
#       three API servers, replays every spec and GET, stops the servers and
#       writes report.json and summary.md under --out.
#
# Environment:
#   ATUS_EQ_BASELINE_DIR      baseline checkout (default ~/.cache/atus/baseline-<commit>)
#   ATUS_EQ_BASELINE_COMMIT   baseline commit (default 387fc2d)
#   ATUS_EQ_PYTHON            interpreter with this checkout installed (default .venv/bin/python)
#   ATUS_EQ_FULL_DATABASE_URL full database (default: the local development database)
#   ATUS_EQ_SERVING_DATABASE_URL  serving database, unless --serving-secrets-file is given
#
# Connection strings are read from the environment or the secrets file only;
# nothing here prints them.
set -euo pipefail
set +x

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
commit="${ATUS_EQ_BASELINE_COMMIT:-387fc2d}"
baseline="${ATUS_EQ_BASELINE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/atus/baseline-$commit}"

python="${ATUS_EQ_PYTHON:-}"
if [[ -z "$python" ]]; then
  if [[ -x "$repo/.venv/bin/python" ]]; then
    python="$repo/.venv/bin/python"
  else
    python="$(command -v python3)"
  fi
fi

setup_baseline() {
  if [[ ! -e "$baseline/src/atus_pipeline/cli.py" ]]; then
    echo "baseline: adding a detached worktree of $commit at $baseline" >&2
    mkdir -p "$(dirname "$baseline")"
    git -C "$repo" worktree add --detach "$baseline" "$commit" >&2
  fi
  local head
  head="$(git -C "$baseline" rev-parse HEAD)"
  if [[ "$head" != "$commit"* ]]; then
    echo "baseline: $baseline is at ${head:0:12}, not $commit" >&2
    exit 2
  fi
  if [[ ! -x "$baseline/.venv/bin/python" ]]; then
    echo "baseline: creating its virtualenv (locked dependencies)" >&2
    if command -v uv >/dev/null 2>&1; then
      uv venv --quiet --python "$python" "$baseline/.venv"
      uv pip install --quiet --python "$baseline/.venv/bin/python" --require-hashes \
        -r "$repo/requirements/api.lock.txt"
      uv pip install --quiet --python "$baseline/.venv/bin/python" --no-deps -e "$baseline"
    else
      "$python" -m venv "$baseline/.venv"
      "$baseline/.venv/bin/python" -m pip install --quiet --require-hashes \
        -r "$repo/requirements/api.lock.txt"
      "$baseline/.venv/bin/python" -m pip install --quiet --no-deps -e "$baseline"
    fi
  fi
}

if [[ "${1:-}" == "setup" ]]; then
  setup_baseline
  echo "$baseline"
  exit 0
fi

targets="abc"
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in
    --targets) targets="${args[$((i + 1))]:-}" ;;
    --targets=*) targets="${args[$i]#--targets=}" ;;
  esac
done
if [[ "$targets" == *a* ]]; then
  setup_baseline
fi

exec "$python" "$here/replay.py" --baseline-dir "$baseline" --baseline-commit "$commit" "$@"
