#!/usr/bin/env bash
# Generate the edge Worker's secrets and the API's ops token (ADR-011) without
# ever printing them.
#
#   scripts/ops/gen_edge_secrets.sh [--file PATH] [--render-origin URL] [--rotate NAME]...
#
# Writes EDGE_AUTH_SECRET, OPS_TOKEN, IP_HMAC_KEY and ATUS_OPS_TOKEN -- each 256
# bits from the operating system's CSPRNG, hex-encoded -- to
# ~/.config/atus/edge.env (override with --file or ATUS_EDGE_ENV_FILE). A new
# directory is created 0700; the file is always 0600 and replaced atomically.
#
# The first three are Worker secrets. ATUS_OPS_TOKEN is the API's bearer for
# /ready and /metrics: it is never loaded into the Worker, and goes to the
# Render environment only (no GitHub workflow calls the origin's /ready or
# /metrics). The Worker's OPS_TOKEN is also the GitHub secret EDGE_OPS_TOKEN
# (environment `ops`; optionally `production`), the names .github/workflows/
# reads. Every generated secret thus has one home: this file.
#
# Re-running keeps every existing value. `--rotate NAME` (EDGE_AUTH_SECRET,
# OPS_TOKEN, IP_HMAC_KEY, ATUS_OPS_TOKEN or all; repeatable) replaces one.
# Rotating EDGE_AUTH_SECRET keeps the old value as EDGE_AUTH_SECRET_PREVIOUS,
# so the API can accept both while the Worker switches
# (ATUS_EDGE_SECRETS=new,old). `--render-origin https://<service>.onrender.com`
# records RENDER_ORIGIN. Any other NAME=value lines already in the file
# (NEON_API_KEY, say) are kept.
#
# Output: the file path, which names were created, kept or rotated, and the
# commands that read each value from the file (`wrangler secret put` for the
# Worker, `gh secret set` for GitHub). Values never reach stdout, stderr or
# any process's arguments.

set -euo pipefail
umask 077

MANAGED_RE='^(EDGE_AUTH_SECRET|EDGE_AUTH_SECRET_PREVIOUS|OPS_TOKEN|IP_HMAC_KEY|ATUS_OPS_TOKEN|RENDER_ORIGIN)='
GENERATED="EDGE_AUTH_SECRET OPS_TOKEN IP_HMAC_KEY ATUS_OPS_TOKEN"
# Loaded into the Worker; ATUS_OPS_TOKEN belongs to the API (Render only).
WORKER_SECRETS="EDGE_AUTH_SECRET OPS_TOKEN IP_HMAC_KEY"

die() {
  printf 'gen_edge_secrets: %s\n' "$1" >&2
  exit 1
}

usage() {
  sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
}

file="${ATUS_EDGE_ENV_FILE:-${HOME}/.config/atus/edge.env}"
render_origin=""
rotate=" "

while [[ $# -gt 0 ]]; do
  case "$1" in
    --file)
      [[ $# -ge 2 ]] || die "--file needs a path"
      file="$2"
      shift 2
      ;;
    --render-origin)
      [[ $# -ge 2 ]] || die "--render-origin needs a URL"
      render_origin="$2"
      shift 2
      ;;
    --rotate)
      [[ $# -ge 2 ]] || die "--rotate needs a name"
      case "$2" in
        EDGE_AUTH_SECRET | OPS_TOKEN | IP_HMAC_KEY | ATUS_OPS_TOKEN) rotate="${rotate}$2 " ;;
        all) rotate=" ${GENERATED} " ;;
        *) die "--rotate takes EDGE_AUTH_SECRET, OPS_TOKEN, IP_HMAC_KEY, ATUS_OPS_TOKEN or all" ;;
      esac
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

if [[ -n "$render_origin" ]]; then
  render_origin="${render_origin%/}"
  [[ "$render_origin" =~ ^https://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?$ ]] ||
    die "--render-origin must be an https:// origin with no path, e.g. https://atus-api-abc123.onrender.com"
fi

dir="$(dirname -- "$file")"
if [[ ! -d "$dir" ]]; then
  mkdir -p -- "$dir"
  chmod 700 "$dir"
fi
if [[ -e "$file" || -L "$file" ]]; then
  [[ ! -L "$file" ]] || die "refusing to write through a symlink: $file"
  [[ -f "$file" ]] || die "not a regular file: $file"
  [[ -O "$file" ]] || die "not owned by the current user: $file"
fi

# The value of NAME in the existing file (last occurrence), or nothing.
current() {
  [[ -f "$file" ]] || return 0
  sed -n "s/^$1=//p" "$file" | tail -n 1
}

# 32 bytes (256 bits) from the kernel CSPRNG as 64 lowercase hex characters.
random_hex() {
  local hex
  hex="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
  [[ "$hex" =~ ^[0-9a-f]{64}$ ]] || die "could not read 32 random bytes from /dev/urandom"
  printf '%s' "$hex"
}

report=""
rotated=" "
edge_previous="$(current EDGE_AUTH_SECRET_PREVIOUS)"
for name in $GENERATED; do
  value="$(current "$name")"
  if [[ -z "$value" ]]; then
    value="$(random_hex)"
    report="${report}  ${name}: created\n"
  elif [[ "$rotate" == *" ${name} "* ]]; then
    [[ "$name" != EDGE_AUTH_SECRET ]] || edge_previous="$value"
    value="$(random_hex)"
    rotated="${rotated}${name} "
    report="${report}  ${name}: rotated\n"
  else
    report="${report}  ${name}: kept\n"
  fi
  printf -v "value_${name}" '%s' "$value"
done

origin="$(current RENDER_ORIGIN)"
if [[ -n "$render_origin" ]]; then
  origin="$render_origin"
  report="${report}  RENDER_ORIGIN: set\n"
elif [[ -n "$origin" ]]; then
  report="${report}  RENDER_ORIGIN: kept\n"
else
  report="${report}  RENDER_ORIGIN: not set (rerun with --render-origin once the Render service exists)\n"
fi

tmp="$(mktemp "${dir}/.edge.env.XXXXXX")"
trap 'rm -f -- "$tmp"' EXIT
# The value_* variables are assigned by `printf -v` in the loop above.
# shellcheck disable=SC2154
{
  printf '# ATUS edge Worker secrets (ADR-011), written by scripts/ops/gen_edge_secrets.sh.\n'
  printf '# Never commit, paste or print these values.\n'
  if [[ -f "$file" ]]; then
    grep -v -E "${MANAGED_RE}|^#|^[[:space:]]*$" "$file" || true
  fi
  printf 'EDGE_AUTH_SECRET=%s\n' "$value_EDGE_AUTH_SECRET"
  if [[ -n "$edge_previous" ]]; then
    printf 'EDGE_AUTH_SECRET_PREVIOUS=%s\n' "$edge_previous"
  fi
  printf 'OPS_TOKEN=%s\n' "$value_OPS_TOKEN"
  printf 'IP_HMAC_KEY=%s\n' "$value_IP_HMAC_KEY"
  printf 'ATUS_OPS_TOKEN=%s\n' "$value_ATUS_OPS_TOKEN"
  if [[ -n "$origin" ]]; then
    printf 'RENDER_ORIGIN=%s\n' "$origin"
  fi
} >"$tmp"
chmod 600 "$tmp"
mv -f -- "$tmp" "$file"
trap - EXIT

edge_dir="$(cd -- "$(dirname -- "$0")/../.." && pwd)/edge"
quoted_file="$(printf '%q' "$file")"
loadable="$WORKER_SECRETS"
[[ -z "$origin" ]] || loadable="$loadable RENDER_ORIGIN"

printf 'Wrote %s (mode 600). Values are not printed.\n' "$file"
printf '%b' "$report"
printf '\nLoad them into the Worker (each command reads its value from the file):\n\n'
printf '  cd %q\n' "$edge_dir"
for name in $loadable; do
  printf "  grep '^%s=' %s | cut -d= -f2- | npx --no-install wrangler secret put %s\n" "$name" "$quoted_file" "$name"
done
# A brand-new Worker's first `wrangler deploy` must carry the secrets that
# wrangler.jsonc lists as required. --secrets-file uploads every line of the
# file it reads, so it gets the Worker's names only, never ATUS_OPS_TOKEN or
# EDGE_AUTH_SECRET_PREVIOUS: a filtered copy in a private temporary directory
# (umask 077), removed when the command ends. Not a process substitution
# (`--secrets-file <(grep ...)`): on macOS, Node starts child processes with
# only stdin, stdout and stderr open, so wrangler's CLI process (which npx and
# wrangler's own launcher start) cannot open /dev/fd/N and the deploy fails.
worker_names="^(${WORKER_SECRETS// /|}|RENDER_ORIGIN)="
# The deploy publishes ../frontend/dist (wrangler.jsonc assets), which a fresh
# clone lacks (gitignored): build it same-origin first, and install edge/'s
# locked tools so that npx runs the pinned version, not a fresh download.
frontend_dir="$(cd -- "$edge_dir/.." && pwd)/frontend"
printf '\nA new Worker'"'"'s first deploy must carry its required secrets (Worker names only).\n'
printf 'It publishes the SPA build, which a fresh clone lacks, with the versions locked in\n'
printf 'edge/package-lock.json, so first:\n\n'
printf '  (cd %q && npm ci && VITE_API_BASE_URL= npm run build)\n' "$frontend_dir"
printf '  cd %q && npm ci\n' "$edge_dir"
# shellcheck disable=SC2016  # $tmp expands when the owner runs the printed command
printf '  (umask 077 && tmp=$(mktemp -d) && trap %s EXIT && grep -E %s %s > "$tmp/worker.env" && npx --no-install wrangler deploy --secrets-file "$tmp/worker.env")\n' \
  "'rm -rf -- \"\$tmp\"'" "'${worker_names}'" "$quoted_file"
[[ -n "$origin" ]] || printf '  (needs RENDER_ORIGIN first: rerun with --render-origin)\n'
printf '\nThe API must accept the same edge secret: set Render env ATUS_EDGE_SECRETS to the\n'
printf 'EDGE_AUTH_SECRET value, copied from the file (never pasted into chat).\n'
printf '\nATUS_OPS_TOKEN is the API'"'"'s ops bearer (/ready, /metrics), never a Worker secret\n'
printf 'and never a GitHub secret (no workflow calls the origin'"'"'s /ready or /metrics):\n'
printf '  Render -> Environment: ATUS_OPS_TOKEN, copied from the file\n'
printf 'The Worker'"'"'s OPS_TOKEN is also the GitHub secret EDGE_OPS_TOKEN: environment ops\n'
printf '(the daily quota report) and, optionally, production (the post-deploy smoke then\n'
printf 'also reads the governor'"'"'s budget):\n'
for environment in ops production; do
  printf "    grep '^OPS_TOKEN=' %s | cut -d= -f2- | gh secret set EDGE_OPS_TOKEN --env %s\n" \
    "$quoted_file" "$environment"
done
if [[ "$rotated" == *" EDGE_AUTH_SECRET "* ]]; then
  printf 'Rotation order: set ATUS_EDGE_SECRETS=<EDGE_AUTH_SECRET>,<EDGE_AUTH_SECRET_PREVIOUS> and\n'
  printf 'redeploy the API, then load EDGE_AUTH_SECRET into the Worker, then drop the old value.\n'
fi
if [[ "$rotated" == *" OPS_TOKEN "* ]]; then
  printf 'Also update every copy of OPS_TOKEN that monitors use (GitHub secret EDGE_OPS_TOKEN).\n'
fi
if [[ "$rotated" == *" ATUS_OPS_TOKEN "* ]]; then
  printf 'Set the new ATUS_OPS_TOKEN on Render ("Save and deploy"); until it is live there,\n'
  printf 'only the ops checks see 404.\n'
fi
