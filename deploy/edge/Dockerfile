# The Cloudflare Worker (edge/, WS-D2) under `wrangler dev` in local mode, for
# the `edge` profile of docker-compose.prod.yml. Build context: the repository
# root, filtered by Dockerfile.dockerignore next to this file (only edge/ is
# sent). Static assets are bind-mounted from frontend/dist at run time, where
# edge/wrangler.jsonc expects them (../frontend/dist).
#
# Local mode only: wrangler simulates KV and the Durable Object on disk under
# /tmp, needs no Cloudflare login and never deploys. Validated in wave 2, once
# edge/ exists.

# node:22-slim (Debian bookworm, glibc: workerd has no musl build), pinned by
# its multi-arch index digest. A literal FROM line, so Dependabot's docker
# ecosystem can propose digest bumps (it cannot follow ARG-based FROM lines).
FROM node:22-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392

ENV NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false \
    WRANGLER_SEND_METRICS=false

RUN mkdir -p /work/edge /work/frontend/dist && chown -R node:node /work
WORKDIR /work/edge
USER node

# The lockfile pins wrangler/workerd with integrity hashes; `npm ci` fails if
# it drifts from package.json.
COPY --chown=node:node edge/package.json edge/package-lock.json ./
RUN npm ci

COPY --chown=node:node edge/ ./
COPY --chmod=0755 deploy/edge/entrypoint.sh /usr/local/bin/atus-edge-entrypoint
COPY --chmod=0644 deploy/edge/relay.mjs /usr/local/lib/atus-edge-relay.mjs

EXPOSE 8787
ENTRYPOINT ["/usr/local/bin/atus-edge-entrypoint"]
