#!/bin/sh
# Start the ATUS Worker with `wrangler dev` (local mode) inside the edge container.
#
# Worker secrets reach wrangler through .dev.vars (mode 600, inside the
# container only), never through the command line, where `ps` would show
# them. Worker vars that deploy.yml publishes are passed as --var when set;
# unset ones keep the values in edge/wrangler.jsonc.
set -eu

umask 077
: > .dev.vars
for name in EDGE_AUTH_SECRET OPS_TOKEN IP_HMAC_KEY RENDER_ORIGIN NEON_API_KEY; do
  eval "value=\${$name:-}"
  if [ -n "$value" ]; then
    printf '%s=%s\n' "$name" "$value" >> .dev.vars
  fi
done

set --
for name in DATA_VERSION ANALYTICS_VERSION RESPONSE_FORMAT SNAPSHOT_ID EPOCH \
            PREWAKE GATE_INFLIGHT API_ORIGIN NEON_PROJECT_ID; do
  eval "value=\${$name:-}"
  if [ -n "$value" ]; then
    set -- "$@" --var "$name:$value"
  fi
done

# The Worker accepts plain HTTP only to a loopback origin (every upstream call
# carries the edge secret). In the stack the API is another container, so
# ATUS_EDGE_UPSTREAM=<host>:<port> starts a loopback relay to it on
# ATUS_EDGE_RELAY_PORT, and RENDER_ORIGIN names http://127.0.0.1:<that port>.
if [ -n "${ATUS_EDGE_UPSTREAM:-}" ]; then
  node /usr/local/lib/atus-edge-relay.mjs "127.0.0.1:${ATUS_EDGE_RELAY_PORT:-10000}" \
    "$ATUS_EDGE_UPSTREAM" &
fi

# --no-install: run the wrangler the lockfile installed, never a download.
exec npx --no-install wrangler dev --ip 0.0.0.0 --port 8787 \
  --persist-to /tmp/wrangler-state --show-interactive-dev-session=false "$@"
