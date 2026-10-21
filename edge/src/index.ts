/**
 * ATUS edge Worker (ADR-011, ADR-015).
 *
 * Static assets (the SPA and the result snapshot) never reach this code:
 * Cloudflare serves them directly, and `run_worker_first` sends only `/api`
 * and `/api/*` here. For those, in order:
 *
 * 1. the allowlist (routes.ts): anything else is a 404 envelope, wrong
 *    methods a 405, with no origin call;
 * 2. Worker-answered routes: `/api/_edge/health`, `/api/v1/wake` and the
 *    bearer-gated `/api/_ops/budget`;
 * 3. metadata GETs: the static snapshot (snapshot.ts) answers them when it
 *    holds them;
 * 4. limits on analysis POSTs: `application/json` only (415), 64 KiB (413);
 * 5. analysis POSTs: the KV result cache (cache.ts) — a hit is answered
 *    here — then, for everything that would reach Render (the prewake
 *    included), the owner's pause switch (pause.ts: 503 `api_paused`), then
 *    admission by the quota governor (admission.ts), which can
 *    refuse with 429/503 or, when unreachable, fail closed; metadata GETs
 *    the snapshot could not answer are admitted as governed lookups (counted,
 *    and charged compute when the API reports DB time);
 * 6. the origin call (upstream.ts) with a fresh header set (proxy.ts); an
 *    admitted analysis's or lookup's settle is queued (`waitUntil`) before it
 *    starts, so a client that disconnects meanwhile does not lose it;
 * 7. the response: API envelopes byte for byte, platform pages converted,
 *    security headers on every response this code produces (errors the
 *    runtime gives before the Worker runs — an unknown HTTP method, a
 *    malformed Content-Length — are the platform's; docs/security.md R8);
 *    then, through `waitUntil`, the KV store (when the store gate allows).
 *
 * One structured log line per /api request: route, status, outcome, cache
 * and governor results, origin status and attempts, the origin's
 * Server-Timing and the edge duration. Never bodies, client IPs, prefix
 * hashes, secrets or the origin hostname.
 */

import {
  NOT_REACHED,
  admitAnalysis,
  admitLookup,
  costClass,
  ipPrefix,
  prefixHmac,
  settleAnalysis,
  settleReportFor,
} from './admission'
import {
  cacheScope,
  cachedMeta,
  kvKey,
  readCached,
  requestHash,
  storeCached,
  storeHeadersAllow,
} from './cache'
import {
  EDGE_VERSION,
  KV_MAX_VALUE_BYTES,
  MAX_BODY_BYTES,
  RETRY_AFTER_UNAVAILABLE_S,
} from './constants'
import type { Denied, SettleReport } from './governor'
import {
  errorResponse,
  isJsonContentType,
  jsonResponse,
  newRequestId,
  notFoundResponse,
  readCapped,
} from './http'
import { capacityUnavailable, handleBudget } from './ops'
import { apiPause, apiPausedResponse } from './pause'
import { buildUpstreamHeaders, relayResponse, upstreamUnavailable } from './proxy'
import { isApiPath, matchRoute, upstreamPathFor, type Route } from './routes'
import { readSnapshot } from './snapshot'
import { fetchOrigin, originBreaker, originUrl } from './upstream'
import { handleWake } from './wake'

export { Governor } from './governor'

interface LogRecord {
  event: 'api'
  request_id: string
  method: string
  route: string | null
  status?: number
  /** edge | pass | waking | overloaded | unavailable | circuit_open | connect_failed | rejected | denied | paused | error */
  outcome?: string
  /** For `unavailable`: the `upstream_unavailable` reason. */
  reason?: string
  /**
   * Analysis POSTs: edge (KV hit) | miss | error (KV read failed) | off (no release vars,
   * or no IP_HMAC_KEY to tag entries with).
   * Metadata GETs: snapshot (answered from it) | miss (sent to the origin).
   */
  cache?: string
  /** Analysis POSTs and metadata lookups: admitted | denied:<budget> | unavailable */
  governor?: string
  upstream_status?: number
  attempts?: number
  server_timing?: string
  duration_ms?: number
  error?: string
}

export default {
  async fetch(request, env, ctx): Promise<Response> {
    const url = new URL(request.url)
    // run_worker_first sends /api paths here. Cloudflare also invokes the
    // Worker for a non-navigation request that matches no asset (an unfurler
    // fetching a share link, a stale tab's missing /snap file; metered,
    // ADR-011 "Request flow"); locally, wrangler dev treats those as
    // navigations. The assets binding answers them with the SPA fallback.
    if (!isApiPath(url.pathname)) return env.ASSETS.fetch(request)
    return handleApi(request, url, env, ctx)
  },
} satisfies ExportedHandler<Env>

async function handleApi(
  request: Request,
  url: URL,
  env: Env,
  ctx: ExecutionContext,
): Promise<Response> {
  const started = Date.now()
  const requestId = newRequestId()
  const log: LogRecord = {
    event: 'api',
    request_id: requestId,
    method: request.method,
    route: null,
  }
  let response: Response
  try {
    response = await dispatch(request, url, env, ctx, requestId, log)
  } catch (err) {
    log.outcome = 'error'
    log.error = err instanceof Error ? err.name : 'Error'
    response = errorResponse(
      500,
      'internal_error',
      'An unexpected internal error occurred.',
      requestId,
    )
  }
  log.status = response.status
  log.duration_ms = Date.now() - started
  console.log(JSON.stringify(log))
  return response
}

async function dispatch(
  request: Request,
  url: URL,
  env: Env,
  ctx: ExecutionContext,
  requestId: string,
  log: LogRecord,
): Promise<Response> {
  const match = matchRoute(request.method, url.pathname)
  if ('error' in match) {
    log.outcome = 'rejected'
    if (match.error === 'not_found') return notFoundResponse(requestId)
    return errorResponse(
      405,
      'method_not_allowed',
      `This API route only accepts ${match.allow} requests.`,
      requestId,
      { headers: { Allow: match.allow }, details: { allowed: [match.allow] } },
    )
  }
  const { route } = match
  log.route = route.name

  if (route.kind === 'edge-health') {
    log.outcome = 'edge'
    return edgeHealth(env, requestId)
  }
  if (route.kind === 'ops-budget') {
    log.outcome = 'edge'
    return handleBudget(request, env, requestId)
  }

  // Tier L0: the deployed snapshot answers metadata without the origin.
  if (route.metadata) {
    const answered = await readSnapshot(env, url, requestId)
    log.cache = answered === null ? 'miss' : 'snapshot'
    if (answered !== null) {
      log.outcome = 'edge'
      return answered
    }
  }

  let body: Uint8Array | undefined
  if (route.method === 'POST') {
    const limited = await readJsonBody(request, requestId)
    if (limited instanceof Response) {
      log.outcome = 'rejected'
      return limited
    }
    body = limited
  }

  // Tier L1: this exact request already answered for this release, under a
  // tag that verifies with the current IP_HMAC_KEY.
  let cacheKey: string | null = null
  if (route.analysis && body !== undefined) {
    const scope = env.IP_HMAC_KEY ? await cacheScope(env) : null
    if (scope === null) {
      log.cache = 'off'
    } else {
      cacheKey = kvKey(scope, await requestHash(url.pathname, url.search.slice(1), body))
      const cached = await readCached(
        env.RESULT_CACHE,
        cacheKey,
        env,
        requestId,
        env.IP_HMAC_KEY,
      )
      if (cached instanceof Response) {
        log.cache = 'edge'
        log.outcome = 'edge'
        return cached
      }
      log.cache = cached
    }
  }

  // The owner's pause switch: while it is on, nothing below reaches Render.
  const pause = apiPause(env, Date.now())
  if (pause !== null) {
    log.outcome = 'paused'
    if (pause.unreadable) log.error = 'api_paused_until_unreadable'
    return apiPausedResponse(pause, requestId)
  }

  const target = originUrl(
    env.RENDER_ORIGIN,
    upstreamPathFor(route, url.pathname),
    route.kind === 'proxy' ? url.search : '',
  )
  if (target === null || !env.EDGE_AUTH_SECRET) {
    // A deploy without RENDER_ORIGIN or EDGE_AUTH_SECRET (wrangler normally refuses one).
    log.outcome = 'unavailable'
    log.error = 'origin_not_configured'
    return upstreamUnavailable(requestId, 502, RETRY_AFTER_UNAVAILABLE_S, 'not_configured')
  }

  if (route.kind === 'wake') {
    log.outcome = 'edge'
    return handleWake(request, env, ctx, requestId, target, env.EDGE_AUTH_SECRET)
  }

  // What reaches Render through here is admitted by the governor: uncached
  // analyses (Render and Neon), and metadata the snapshot could not answer
  // (Render's outbound bandwidth, and Neon when it is a freshly started API
  // process's first data request). Health and wake are not.
  let leaseId: string | null = null
  const analysis = route.analysis === true && body !== undefined
  if (analysis || route.metadata) {
    // While the breaker skips the origin, an admission could only be refunded:
    // answer now and spend no governor call.
    const openMs = originBreaker.remainingOpenMs(Date.now())
    if (openMs > 0) {
      log.outcome = 'circuit_open'
      return upstreamUnavailable(requestId, 503, openMs / 1000, 'circuit_open')
    }
    if (!env.IP_HMAC_KEY) {
      // Without the key no client can be charged (wrangler normally refuses such a deploy).
      log.governor = 'unavailable'
      log.outcome = 'denied'
      log.error = 'ip_hmac_key_not_configured'
      return capacityUnavailable(requestId)
    }
    const prefix = await prefixHmac(
      env.IP_HMAC_KEY,
      Date.now(),
      ipPrefix(request.headers.get('cf-connecting-ip')),
    )
    const admission =
      analysis && body !== undefined
        ? await admitAnalysis(env, { prefix, costClass: costClass(route.name, body) })
        : await admitLookup(env, { prefix })
    if (admission.kind === 'unavailable') {
      log.governor = 'unavailable'
      log.outcome = 'denied'
      return capacityUnavailable(requestId)
    }
    if (admission.kind === 'denied') {
      log.governor = `denied:${admission.denial.budget}`
      log.outcome = 'denied'
      return denialResponse(admission.denial, requestId, !analysis)
    }
    log.governor = 'admitted'
    leaseId = admission.leaseId
  }

  const forwarding = forward(request, route, target, body, env, requestId, log, cacheKey)
  if (leaseId !== null) {
    // Queued before the origin answers: Cloudflare may cancel a request's
    // pending work once its client disconnects, but gives `waitUntil` work
    // up to 30 s more. A settle lost anyway expires and is charged then.
    const lease = leaseId
    ctx.waitUntil(
      forwarding.then(
        (forwarded) => settleAnalysis(env, lease, forwarded.settle),
        () => false,
      ),
    )
  }
  const forwarded = await forwarding
  const { store } = forwarded
  if (store !== undefined) {
    ctx.waitUntil(
      storeCached(env.RESULT_CACHE, store.key, store.body, store.meta, env.IP_HMAC_KEY),
    )
  }
  return forwarded.response
}

/**
 * The body of an analysis POST, or the 413/415 envelope that rejects it.
 * A declared Content-Length over the cap is refused before reading; an
 * undeclared or understated one is cut off while streaming.
 */
async function readJsonBody(
  request: Request,
  requestId: string,
): Promise<Uint8Array | Response> {
  const tooLarge = (): Response =>
    errorResponse(
      413,
      'payload_too_large',
      `The request body exceeds the ${MAX_BODY_BYTES}-byte limit.`,
      requestId,
      { details: { max_bytes: MAX_BODY_BYTES } },
    )
  if (Number(request.headers.get('content-length') ?? 0) > MAX_BODY_BYTES) return tooLarge()
  if (!isJsonContentType(request.headers.get('content-type'))) {
    return errorResponse(
      415,
      'unsupported_media_type',
      'Analysis requests must be sent with Content-Type: application/json.',
      requestId,
    )
  }
  const bytes = await readCapped(request.body, MAX_BODY_BYTES)
  return bytes ?? tooLarge()
}

interface Forwarded {
  response: Response
  /** How the origin call ended, for the governor (admitted analyses only). */
  settle: SettleReport
  /** A response that passed the whole store gate. */
  store?: { key: string; body: Uint8Array; meta: ReturnType<typeof cachedMeta> }
}

async function forward(
  request: Request,
  route: Route,
  target: string,
  body: Uint8Array | undefined,
  env: Env,
  requestId: string,
  log: LogRecord,
  cacheKey: string | null,
): Promise<Forwarded> {
  const outcome = await fetchOrigin(target, {
    method: route.method,
    headers: buildUpstreamHeaders(request, requestId, env.EDGE_AUTH_SECRET, body !== undefined),
    body,
    // Never follow: a redirect would resend the edge secret wherever it points.
    redirect: 'manual',
  })
  if (outcome.kind === 'circuit-open') {
    log.outcome = 'circuit_open'
    return {
      response: upstreamUnavailable(
        requestId,
        503,
        outcome.retryAfterMs / 1000,
        'circuit_open',
      ),
      settle: NOT_REACHED,
    }
  }
  if (outcome.kind === 'connect-failed') {
    log.outcome = 'connect_failed'
    log.attempts = outcome.attempts
    log.error = outcome.error
    return {
      response: upstreamUnavailable(
        requestId,
        502,
        RETRY_AFTER_UNAVAILABLE_S,
        'connect_failed',
      ),
      settle: NOT_REACHED,
    }
  }
  const { response: upstream, attempts } = outcome
  log.attempts = attempts
  log.upstream_status = upstream.status
  const timing = upstream.headers.get('server-timing')
  if (timing !== null) log.server_timing = timing

  // Store gate, header half: only then is the body buffered (<= 16 KiB) to keep a copy.
  const storable = cacheKey !== null && storeHeadersAllow(upstream, env)
  const relayed = await relayResponse(
    upstream,
    requestId,
    storable ? { bufferUpTo: KV_MAX_VALUE_BYTES } : {},
  )
  log.outcome = relayed.outcome
  if (relayed.reason !== undefined) log.reason = relayed.reason
  const forwarded: Forwarded = {
    response: relayed.response,
    settle: settleReportFor(upstream, relayed, attempts),
  }
  if (storable && relayed.outcome === 'pass' && relayed.body !== undefined) {
    forwarded.store = {
      key: cacheKey,
      body: relayed.body,
      meta: cachedMeta(upstream, Date.now()),
    }
  }
  return forwarded
}

/**
 * A governor denial as the API's envelope: 429 for a client's budget, 503 for
 * shared ones. A metadata lookup's says so, whichever budget refused it.
 */
function denialResponse(denial: Denied, requestId: string, lookup: boolean): Response {
  const messages: Record<Denied['code'], string> = {
    rate_limited:
      'Your network has started as many new analyses today as this free service allows. ' +
      'Saved and prewarmed results still work; the limit resets at 00:00 UTC.',
    daily_capacity_reached:
      "Today's free capacity for new analyses is used up. It resets at 00:00 UTC.",
    capacity_paused:
      "New analyses are paused until this month's free database allowance allows more.",
  }
  const metadata: Record<Denied['code'], string> = {
    rate_limited:
      'Your network has made as many uncached metadata requests today as this free service ' +
      "allows. The site's metadata files still work; the limit resets at 00:00 UTC.",
    daily_capacity_reached:
      "Today's free capacity for uncached metadata requests is used up. The site's metadata " +
      'files still work; it resets at 00:00 UTC.',
    capacity_paused:
      "Uncached metadata requests are paused until this month's free database allowance " +
      "allows more. The site's metadata files still work.",
  }
  return errorResponse(
    denial.code === 'rate_limited' ? 429 : 503,
    denial.code,
    (lookup ? metadata : messages)[denial.code],
    requestId,
    {
      retryAfterSeconds: denial.retryAfterSeconds,
      details: { scope: denial.scope, budget: denial.budget, resumes_at: denial.resumesAt },
    },
  )
}

/** GET /api/_edge/health: answered here, never touching Render or Neon. */
async function edgeHealth(env: Env, requestId: string): Promise<Response> {
  const pause = apiPause(env, Date.now())
  return jsonResponse(
    200,
    {
      status: 'ok',
      edge_version: EDGE_VERSION,
      api_version: env.API_VERSION,
      analytics_version: env.ANALYTICS_VERSION || null,
      data_version: env.DATA_VERSION || null,
      response_format: env.RESPONSE_FORMAT,
      snapshot_id: env.SNAPSHOT_ID || null,
      cache_scope: await cacheScope(env),
      api_paused: pause === null ? null : { until: pause.until, reason: pause.reason },
    },
    requestId,
  )
}
