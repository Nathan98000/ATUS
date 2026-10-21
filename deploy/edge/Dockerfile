# The Cloudflare Worker (edge/, WS-D2) under `wrangler dev` in local mode, for
# the `edge` profile of docker-compose.prod.yml. Build context: the repository
# root, filtered by Dockerfile.dockerignore next to this file (only edge/ is
# sent). Static assets are bind-mounted from frontend/dist at run time, where
# edge/wrangler.jsonc expects them (../frontend/dist).
#
# Local mode only: wrangler simulates KV and the Durable Object on disk under
# /tmp, needs no Cloudflare login and never deploys. It makes no call to
# Cloudflare either: no usage metrics or error reports, and no fetch of the
# request.cf template (CLOUDFLARE_CF_FETCH_ENABLED=false; Miniflare's built-in
# fallback is used).

# node:22-slim (Debian bookworm, glibc: workerd has no musl build), pinned by
# its multi-arch index digest.
ARG NODE_BASE=node:22-slim
ARG NODE_DIGEST=sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392

FROM ${NODE_BASE}@${NODE_DIGEST}

ENV NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false \
    WRANGLER_SEND_METRICS=false \
    WRANGLER_SEND_ERROR_REPORTS=false \
    WRANGLER_HIDE_BANNER=true \
    CLOUDFLARE_CF_FETCH_ENABLED=false

RUN mkdir -p /work/edge /work/frontend/dist && chown -R node:node /work
WORKDIR /work/edge
USER node

# The lockfile pins wrangler/workerd with integrity hashes; `npm ci` fails if
# it drifts from package.json.
COPY --chown=node:node edge/package.json edge/package-lock.json ./
RUN npm ci

COPY --chown=node:node edge/ ./
COPY --chmod=0755 deploy/edge/entrypoint.sh /usr/local/bin/atus-edge-entrypoint
COPY --chmod=0755 deploy/edge/origin-relay.mjs /usr/local/bin/atus-edge-origin-relay.mjs

EXPOSE 8787
ENTRYPOINT ["/usr/local/bin/atus-edge-entrypoint"]
