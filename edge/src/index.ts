/**
 * ATUS edge Worker (ADR-011).
 *
 * Static assets (the SPA and the result snapshot) never reach this code:
 * Cloudflare serves them directly, and `run_worker_first` sends only `/api`
 * and `/api/*` here. For those, in order:
 *
 * 1. the allowlist (routes.ts): anything else is a 404 envelope, wrong
 *    methods a 405, with no origin call;
 * 2. Worker-answered routes: `/api/_edge/health` and `/api/v1/wake`;
 * 3. limits on analysis POSTs: `application/json` only (415), 64 KiB (413);
 * 4. the origin call (upstream.ts) with a fresh header set (proxy.ts);
 * 5. the response: API envelopes byte for byte, platform pages converted,
 *    security headers on everything.
 *
 * One structured log line per /api request: route, status, outcome, origin
 * status and attempts, the origin's Server-Timing and the edge duration.
 * Never bodies, client IPs, secrets or the origin hostname.
 */

import { EDGE_VERSION, MAX_BODY_BYTES } from './constants'
import {
  errorResponse,
  isJsonContentType,
  jsonResponse,
  newRequestId,
  readCapped,
} from './http'
import { buildUpstreamHeaders, relayResponse, upstreamUnavailable } from './proxy'
import { isApiPath, matchRoute, upstreamPathFor, type Route } from './routes'
import { fetchOrigin, originUrl } from './upstream'
import { handleWake } from './wake'

export { Governor } from './governor'

interface LogRecord {
  event: 'api'
  request_id: string
  method: string
  route: string | null
  status?: number
  /** edge | pass | waking | unavailable | circuit_open | connect_failed | rejected | error */
  outcome?: string
  upstream_status?: number
  attempts?: number
  server_timing?: string
  duration_ms?: number
  error?: string
}

export default {
  async fetch(request, env, ctx): Promise<Response> {
    const url = new URL(request.url)
    // Non-navigation requests that match no asset also reach the Worker:
    // the assets binding applies the SPA fallback to them.
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
    if (match.error === 'not_found') {
      return errorResponse(
        404,
        'not_found',
        'The requested API route does not exist.',
        requestId,
      )
    }
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

  let body: Uint8Array | undefined
  if (route.method === 'POST') {
    const limited = await readJsonBody(request, requestId)
    if (limited instanceof Response) {
      log.outcome = 'rejected'
      return limited
    }
    body = limited
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
    return upstreamUnavailable(requestId, 502)
  }

  if (route.kind === 'wake') {
    log.outcome = 'edge'
    return handleWake(request, env, ctx, requestId, target, env.EDGE_AUTH_SECRET)
  }

  return forward(request, route, target, body, env.EDGE_AUTH_SECRET, requestId, log)
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

async function forward(
  request: Request,
  route: Route,
  target: string,
  body: Uint8Array | undefined,
  edgeSecret: string,
  requestId: string,
  log: LogRecord,
): Promise<Response> {
  const outcome = await fetchOrigin(target, {
    method: route.method,
    headers: buildUpstreamHeaders(request, requestId, edgeSecret, body !== undefined),
    body,
    // Never follow: a redirect would resend the edge secret wherever it points.
    redirect: 'manual',
  })
  if (outcome.kind === 'circuit-open') {
    log.outcome = 'circuit_open'
    return upstreamUnavailable(requestId, 503, outcome.retryAfterMs / 1000)
  }
  if (outcome.kind === 'connect-failed') {
    log.outcome = 'connect_failed'
    log.attempts = outcome.attempts
    log.error = outcome.error
    return upstreamUnavailable(requestId, 502)
  }
  const { response: upstream, attempts } = outcome
  log.attempts = attempts
  log.upstream_status = upstream.status
  const timing = upstream.headers.get('server-timing')
  if (timing !== null) log.server_timing = timing
  const relayed = await relayResponse(upstream, requestId)
  log.outcome = relayed.outcome
  return relayed.response
}

/** GET /api/_edge/health: answered here, never touching Render or Neon. */
function edgeHealth(env: Env, requestId: string): Response {
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
    },
    requestId,
  )
}
