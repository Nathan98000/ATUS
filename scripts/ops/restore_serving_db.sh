#!/usr/bin/env bash
# Restore a serving-database release artifact into a new, empty database
# (docs/adr/ADR-013-data-releases-and-serving-db.md, "Release procedure").
#
#   scripts/ops/restore_serving_db.sh --artifact atus-serving-0325-<build_id>.dump
#
# What it does, in order (implemented by `atus restore-serving-db`,
# src/atus_pipeline/serving/restore.py):
#   1. reads the owner's connection string (Neon: neondb_owner, direct endpoint,
#      not the -pooler host) from $ATUS_OWNER_DATABASE_URL or from the secrets
#      file (default ~/.config/atus/neon.env, a line
#      ATUS_OWNER_DATABASE_URL=postgresql://...); it is never printed;
#   2. verifies the dump's SHA-256 against its .sha256 file and its manifest;
#   3. refuses a database that is not empty (one new Neon project per release);
#   4. pg_restore --no-owner --no-acl --single-transaction --exit-on-error,
#      with a client at least as new as the server (local, or the postgres
#      image through docker);
#   5. applies scripts/ops/roles.sql: role atus_api, SELECT-only, with a
#      generated 192-bit password sent as a SCRAM verifier; writes
#      ATUS_API_DB_PASSWORD and ATUS_API_DATABASE_URL back into the secrets
#      file (mode 600);
#   6. VACUUM (ANALYZE) on every table;
#   7. checks the data version, row counts, serving marker, contract, planner
#      statistics, physical order, the API role's defaults, that it cannot
#      write, and the worst-case query plans;
#   8. checks sum(pg_database_size(datname)) against the provider limit
#      (--size-limit-bytes, default 1,000,000,000).
#
# Useful options: --secrets-file PATH, --rotate-api-password, --roles-only,
# --verify-content-hash (re-reads every row: egress on a metered host),
# --report restore-report.json. `--help` lists them all.
#
# Needs the repository's Python environment (`uv venv && uv pip install -e .`)
# or ATUS_BIN pointing at an `atus` executable.

set -euo pipefail
set +x          # never trace: commands below handle secrets
umask 077       # anything written is private to the owner

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
atus="${ATUS_BIN:-}"
if [[ -z "$atus" ]]; then
  if [[ -x "$repo/.venv/bin/atus" ]]; then
    atus="$repo/.venv/bin/atus"
  elif command -v atus >/dev/null 2>&1; then
    atus="$(command -v atus)"
  else
    echo "restore_serving_db.sh: no 'atus' executable; run 'uv venv && uv pip install -e .' in $repo or set ATUS_BIN" >&2
    exit 2
  fi
fi

exec "$atus" restore-serving-db --roles-sql "$repo/scripts/ops/roles.sql" "$@"
