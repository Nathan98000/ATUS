#!/usr/bin/env bash
# Generate the edge Worker's secrets (ADR-011) without ever printing them.
#
#   scripts/ops/gen_edge_secrets.sh [--file PATH] [--render-origin URL] [--rotate NAME]...
#
# Writes EDGE_AUTH_SECRET, OPS_TOKEN and IP_HMAC_KEY -- each 256 bits from the
# operating system's CSPRNG, hex-encoded -- to ~/.config/atus/edge.env
# (override with --file or ATUS_EDGE_ENV_FILE). A new directory is created
# 0700; the file is always 0600 and replaced atomically.
#
# Re-running keeps every existing value. `--rotate NAME` (EDGE_AUTH_SECRET,
# OPS_TOKEN, IP_HMAC_KEY or all; repeatable) replaces one. Rotating
# EDGE_AUTH_SECRET keeps the old value as EDGE_AUTH_SECRET_PREVIOUS, so the
# API can accept both while the Worker switches (ATUS_EDGE_SECRETS=new,old).
# `--render-origin https://<service>.onrender.com` records RENDER_ORIGIN.
# Any other NAME=value lines already in the file (NEON_API_KEY, say) are kept.
#
# Output: the file path, which names were created, kept or rotated, and the
# `wrangler secret put` commands that read each value from the file. Values
# never reach stdout, stderr or any process's arguments.

set -euo pipefail
umask 077

MANAGED_RE='^(EDGE_AUTH_SECRET|EDGE_AUTH_SECRET_PREVIOUS|OPS_TOKEN|IP_HMAC_KEY|RENDER_ORIGIN)='
GENERATED="EDGE_AUTH_SECRET OPS_TOKEN IP_HMAC_KEY"

die() {
  printf 'gen_edge_secrets: %s\n' "$1" >&2
  exit 1
}

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
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
        EDGE_AUTH_SECRET | OPS_TOKEN | IP_HMAC_KEY) rotate="${rotate}$2 " ;;
        all) rotate=" ${GENERATED} " ;;
        *) die "--rotate takes EDGE_AUTH_SECRET, OPS_TOKEN, IP_HMAC_KEY or all" ;;
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
edge_previous="$(current EDGE_AUTH_SECRET_PREVIOUS)"
for name in $GENERATED; do
  value="$(current "$name")"
  if [[ -z "$value" ]]; then
    value="$(random_hex)"
    report="${report}  ${name}: created\n"
  elif [[ "$rotate" == *" ${name} "* ]]; then
    [[ "$name" != EDGE_AUTH_SECRET ]] || edge_previous="$value"
    value="$(random_hex)"
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
  if [[ -n "$origin" ]]; then
    printf 'RENDER_ORIGIN=%s\n' "$origin"
  fi
} >"$tmp"
chmod 600 "$tmp"
mv -f -- "$tmp" "$file"
trap - EXIT

edge_dir="$(cd -- "$(dirname -- "$0")/../.." && pwd)/edge"
quoted_file="$(printf '%q' "$file")"
loadable="$GENERATED"
[[ -z "$origin" ]] || loadable="$loadable RENDER_ORIGIN"

printf 'Wrote %s (mode 600). Values are not printed.\n' "$file"
printf '%b' "$report"
printf '\nLoad them into the Worker (each command reads its value from the file):\n\n'
printf '  cd %q\n' "$edge_dir"
for name in $loadable; do
  printf "  grep '^%s=' %s | cut -d= -f2- | npx wrangler secret put %s\n" "$name" "$quoted_file" "$name"
done
printf '\nThe API must accept the same edge secret: set Render env ATUS_EDGE_SECRETS to the\n'
printf 'EDGE_AUTH_SECRET value, copied from the file (never pasted into chat).\n'
if [[ "$rotate" == *" EDGE_AUTH_SECRET "* ]]; then
  printf 'Rotation order: set ATUS_EDGE_SECRETS=<EDGE_AUTH_SECRET>,<EDGE_AUTH_SECRET_PREVIOUS> and\n'
  printf 'redeploy the API, then load EDGE_AUTH_SECRET into the Worker, then drop the old value.\n'
fi
if [[ "$rotate" == *" OPS_TOKEN "* ]]; then
  printf 'Also update every copy of OPS_TOKEN that monitors use (GitHub environment secrets).\n'
fi
