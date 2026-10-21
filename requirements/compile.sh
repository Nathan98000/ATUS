#!/usr/bin/env bash
# Regenerate the hash-locked requirement files from pyproject.toml.
#
#   requirements/compile.sh                            # keep existing pins that still fit
#   requirements/compile.sh --upgrade                  # newest versions pyproject allows
#   requirements/compile.sh --upgrade-package fastapi  # extra args go to every compile
#
# Outputs (commit all four; never edit them by hand):
#   dev.lock.txt    runtime + dev extra from pyproject.toml, plus the build
#                   backend (CI, contributors)
#   api.lock.txt    API image runtime only, from api.in, constrained to the dev
#                   lock's pins so CI tests exactly the versions the image ships
#   build.lock.txt  the wheel build backend (setuptools), from build.in, with
#                   the dev lock's pin
#   ci-tools.lock.txt  CI-only tools (pip-audit), from ci-tools.in;
#                   installed into their own virtualenv, never next to the app
#
# Install them with hash checking, for example (a development checkout):
#   pip install --require-hashes --no-deps -r requirements/dev.lock.txt
#   pip install --no-deps --no-build-isolation -e .
#   pip check
#
# The locks are "universal" (one file for macOS, Linux and every supported
# Python from 3.11) and carry hashes for every published file of each pinned
# version, so the same file serves contributors, CI and the linux/amd64 image.
# Requires uv (https://docs.astral.sh/uv/) and network access to PyPI.
set -euo pipefail

cd "$(dirname "$0")/.."

common=(
  --universal
  --python-version 3.11
  --generate-hashes
  --custom-compile-command "requirements/compile.sh"
  --quiet
)

uv pip compile "${common[@]}" "$@" pyproject.toml requirements/build.in --extra dev \
  -o requirements/dev.lock.txt
uv pip compile "${common[@]}" "$@" requirements/api.in -c requirements/dev.lock.txt \
  -o requirements/api.lock.txt
uv pip compile "${common[@]}" "$@" requirements/build.in -c requirements/dev.lock.txt \
  -o requirements/build.lock.txt
uv pip compile "${common[@]}" "$@" requirements/ci-tools.in -o requirements/ci-tools.lock.txt

echo "wrote requirements/{dev,api,build,ci-tools}.lock.txt"
