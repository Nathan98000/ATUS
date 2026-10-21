# ATUS edge Worker

The Cloudflare Worker in front of the API (decision and request flow: [ADR-011](../docs/adr/ADR-011-hosting.md);
result cache, snapshot and quota governor: [ADR-015](../docs/adr/ADR-015-edge-cache-snapshot-and-governor.md)).
One `workers.dev` origin serves the SPA build (`../frontend/dist`) as static assets; only `/api` and
`/api/*` run this Worker, which:

- allows exactly the routes in `src/routes.ts` (everything else under `/api` is a 404 envelope);
- refuses analysis POSTs that are not `application/json` (415) or exceed 64 KiB (413);
- forwards to `RENDER_ORIGIN` with a fresh header set: `Content-Type`, `Accept`, `If-None-Match`, plus
  `Accept-Encoding: gzip` (always), `X-Request-ID`, `X-ATUS-Client-IP` (from `CF-Connecting-IP`),
  `X-ATUS-Edge-Auth` and `User-Agent: atus-edge/<version>`;
- sets no timeout, retries once only when the connection fails before response headers, and skips the
  origin for 30 s after 2 consecutive connection failures (per isolate);
- passes API JSON and error envelopes through byte for byte, and turns platform responses into
  `service_waking` (503), `overloaded` (503, uvicorn's own connection-limit answer) or
  `upstream_unavailable` (502/503, with `details.reason`) envelopes with `Retry-After`;
- sets the security headers on every `/api` response it produces and drops `server`, `x-render-*`, `rndr-id`
  and every other origin header not on its allowlist;
- answers `GET /api/_edge/health` itself and handles the `GET /api/v1/wake` prewake;
- answers the metadata GETs (`/api/v1/meta`, `/activities`, `/activities/presets`,
  `/population/metadata`, `/activities/<code>`) without a query string from the deployed static
  snapshot (`src/snapshot.ts`); the rest reach the origin as governed lookups (200 per client
  /24 or /48 and 2,000 in all per UTC day, and the compute budgets below: a lookup that reaches
  a freshly started API process reads from Neon, and is charged like an analysis);
- answers analysis POSTs from Workers KV when the exact request bytes were answered before for the same
  release (`X-Cache: edge`; key `r1/<CACHE_SCOPE>/<k>`, `src/cache.ts`), and stores new 200 results
  the API computed (`X-Cache: miss`) that pass the store gate (≤ 16 KiB, 30-day TTL);
- admits every other analysis through the quota governor, a SQLite Durable Object (`src/governor.ts`):
  60 per client /24 or /48 per UTC day, 300 admissions including refunded ones, and a fifth of the
  day's compute ceiling (429 `rate_limited`), 600 per day and 300 heavy per day, a daily Neon
  CU-hour ceiling and, without the Neon key, a daily egress estimate (503 `daily_capacity_reached`),
  and with the optional Neon key monthly pacing and an egress stop (503 `capacity_paused`). If the
  governor cannot be reached, uncached analyses and metadata lookups get 503 `capacity_unavailable`;
  everything else keeps working;
- answers `GET /api/_ops/budget` with the governor's budget state for `Authorization: Bearer <OPS_TOKEN>`
  (404 for anything else);
- while the owner's pause switch is on (optional secrets `API_PAUSED_UNTIL` and `API_PAUSED_REASON`,
  set by `deploy.yml` operation `pause-api`; `src/pause.ts`), answers every request that would reach
  Render with 503 `api_paused` and the date, and reports the switch in `/api/_edge/health`.

## Commands

```sh
npm ci
npm test              # Vitest in workerd (Miniflare) + Node checks; no Cloudflare account needed
npm run typecheck     # generates worker-configuration.d.ts with `wrangler types`, then tsc
npm run lint
npm run format:check
```

Tests never reach Cloudflare or Render: the secrets are fake, `RENDER_ORIGIN` is
`https://origin.invalid`, and the origin is either a mocked `fetch` or a local HTTP server on an
ephemeral loopback port (`test/global-setup.ts`). `test/node/config.test.ts` asserts the
`wrangler.jsonc` invariants that CI must hold (no `cache` block, `preview_urls: false`,
`workers_dev: true`, `run_worker_first` = `/api` and `/api/*`, SPA fallback).

## Local development

Copy `.dev.vars.example` to `.dev.vars` (gitignored), point `RENDER_ORIGIN` at a local API (plain
HTTP is accepted only for loopback hosts) and start that API with `ATUS_EDGE_SECRETS` set to the same
`EDGE_AUTH_SECRET`. Build the frontend (`npm run build` in `../frontend`), then run `npm run dev`.
`wrangler dev` runs fully locally (KV and the Durable Object are simulated under `.wrangler/`); no
account is needed. To exercise the edge cache, pass the release vars, e.g.
`npx wrangler dev --var DATA_VERSION:<x-data-version of the local API> --var ANALYTICS_VERSION:<its
analytics version>`.

The static result snapshot (`/snap/...`, tier L0) is written into the SPA build by
`scripts/build_snapshot.py` from a running API; see ADR-015.

The whole public origin runs locally in Docker too: `scripts/smoke_stack.sh --edge [--e2e]` starts
this Worker (the `edge` service of `docker-compose.prod.yml`, `wrangler dev` in local mode, no call to
Cloudflare) with the SPA build and a freshly built snapshot, in front of the production-mode API, then
probes it (`deploy/stack/edge_probe.py`) and optionally runs the Playwright suite through it.

## Secrets

Production secrets live only in the Worker's secret store. `scripts/ops/gen_edge_secrets.sh` writes
them to `~/.config/atus/edge.env` (mode 600) and prints the `wrangler secret put` commands that read
each value from that file; it never prints a value. `wrangler.jsonc` lists the required ones, so
`wrangler deploy` refuses to publish without them.
