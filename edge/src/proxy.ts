/**
 * What crosses the Worker in each direction (ADR-011 "Request flow" steps
 * 3.3 and 7, "Proxy chain and client-IP trust").
 *
 * Upstream, the Worker builds a fresh header set from an allowlist; nothing
 * else the client sent is forwarded — in particular no client `X-ATUS-*`,
 * `X-Forwarded-*`, `Forwarded`, `X-Real-IP`, `Cookie` or `Authorization`.
 *
 * Downstream, API responses pass through byte for byte: status, body and an
 * allowlist of headers. Responses that are not the API's — Render's
 * hibernate 503 or HTML loading page, Cloudflare 52x pages, redirects,
 * uvicorn's own plain-text 503 when its connection limit is reached — are
 * replaced by JSON envelopes the SPA understands.
 *
 * `upstream_unavailable` says why in `details.reason`, because only some
 * causes mean the API may have taken (and computed) the request: an
 * `origin_timeout` (Cloudflare 524) or a `broken_response` (the API's body
 * cut off) may have computed it, so a client retries such an analysis once,
 * when it joins the running computation or hits the API's cache; the other
 * reasons (`connect_failed`, `circuit_open`, `platform_error`,
 * `not_configured`) never reached the API and are retried like a cold start.
 */

import {
  EDGE_VERSION,
  MAX_ERROR_BODY_BYTES,
  RETRY_AFTER_OVERLOADED_S,
  RETRY_AFTER_UNAVAILABLE_S,
  RETRY_AFTER_WAKING_S,
} from './constants'
import {
  applySecurityHeaders,
  errorResponse,
  isHtmlContentType,
  isJsonContentType,
  readCapped,
  readUpTo,
} from './http'

/** Client request headers copied upstream (the body's Content-Type only when there is a body). */
const FORWARDED_REQUEST_HEADERS = ['accept', 'accept-encoding', 'if-none-match'] as const

/** IPv4 or IPv6 text form, as Cloudflare writes `CF-Connecting-IP`. */
const CLIENT_IP = /^[0-9A-Fa-f:.]{2,45}$/

/**
 * Origin response headers passed to the client. Everything else — `server`,
 * `x-render-*`, `rndr-id`, `set-cookie`, the origin's caching and CORS
 * headers, `content-length`/`content-encoding` (the runtime re-frames the
 * body) — is dropped.
 */
const RELAYED_RESPONSE_HEADERS = [
  'content-type',
  'etag',
  'last-modified',
  'retry-after',
  'allow',
  'server-timing',
  'x-request-id',
  'x-data-version',
  'x-analytics-version',
  'x-analysis-key',
  'x-cache',
  'x-response-format',
] as const

/** The upstream header set for one origin call. */
export function buildUpstreamHeaders(
  request: Request,
  requestId: string,
  edgeSecret: string,
  hasBody: boolean,
): Headers {
  const headers = new Headers()
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = request.headers.get(name)
    if (value !== null) headers.set(name, value)
  }
  if (hasBody) {
    const contentType = request.headers.get('content-type')
    if (contentType !== null) headers.set('content-type', contentType)
  }
  headers.set('X-Request-ID', requestId)
  // Set by Cloudflare's edge for the visitor; a client cannot forge it on
  // workers.dev. The API trusts it only alongside a valid X-ATUS-Edge-Auth.
  const clientIp = request.headers.get('cf-connecting-ip')
  if (clientIp !== null && CLIENT_IP.test(clientIp)) headers.set('X-ATUS-Client-IP', clientIp)
  headers.set('X-ATUS-Edge-Auth', edgeSecret)
  headers.set('User-Agent', `atus-edge/${EDGE_VERSION}`)
  return headers
}

/** How an origin response reached the client. */
export type RelayOutcome = 'pass' | 'waking' | 'overloaded' | 'unavailable'

/** Why the edge answered `upstream_unavailable` (`details.reason`). */
export type UnavailableReason =
  /** The origin's connection failed twice, before any response headers. */
  | 'connect_failed'
  /** The per-isolate breaker skipped the origin: it was not contacted. */
  | 'circuit_open'
  /** Not the API answering: a platform error page, a redirect, an unreadable envelope. */
  | 'platform_error'
  /** Cloudflare 524: the API took the request and had not answered in time. */
  | 'origin_timeout'
  /** The API's own response broke off mid-body. */
  | 'broken_response'
  /** The Worker has no usable RENDER_ORIGIN or EDGE_AUTH_SECRET. */
  | 'not_configured'

/** uvicorn's answer when its `limit_concurrency` is reached (before the app runs). */
const UVICORN_SHED_BODY = 'Service Unavailable'

/** Cloudflare's "origin did not answer in time" status. */
const ORIGIN_TIMEOUT_STATUS = 524

export interface Relayed {
  response: Response
  outcome: RelayOutcome
  /** Set when `outcome` is `unavailable`. */
  reason?: UnavailableReason
  /** The API's error code, when an error envelope was passed through. */
  errorCode?: string
  /** The complete body of a passed-through 2xx, when it was buffered (`bufferUpTo`). */
  body?: Uint8Array
}

export interface RelayOptions {
  /**
   * Read a 2xx JSON body before answering, if it is at most this many bytes,
   * so the caller can store it. A longer body is streamed on unchanged.
   */
  bufferUpTo?: number
}

/**
 * Turn an origin response into the client response.
 *
 * Passed through unchanged: 2xx JSON (streamed, or buffered when asked),
 * 304, and 4xx/5xx whose body is the API's error envelope. Converted:
 * everything else, to 503 `service_waking` (the origin is starting), 503
 * `overloaded` (uvicorn shed the connection) or 502 `upstream_unavailable`
 * with its reason.
 */
export async function relayResponse(
  upstream: Response,
  requestId: string,
  options: RelayOptions = {},
): Promise<Relayed> {
  const verdict = await judge(upstream, options)
  if (typeof verdict === 'object' && !('unavailable' in verdict)) {
    const relayed: Relayed = {
      response: passThrough(upstream, verdict.body, requestId),
      outcome: 'pass',
    }
    if (verdict.errorCode !== undefined) relayed.errorCode = verdict.errorCode
    if (verdict.complete !== undefined) relayed.body = verdict.complete
    return relayed
  }
  // Free the origin connection; the body is not ours to show.
  if (!upstream.bodyUsed) {
    try {
      await upstream.body?.cancel()
    } catch {
      // Already closed or errored: nothing to release.
    }
  }
  if (verdict === 'waking') return { response: serviceWaking(requestId), outcome: 'waking' }
  if (verdict === 'overloaded') {
    return { response: originOverloaded(requestId), outcome: 'overloaded' }
  }
  return {
    response: upstreamUnavailable(
      requestId,
      502,
      RETRY_AFTER_UNAVAILABLE_S,
      verdict.unavailable,
    ),
    outcome: 'unavailable',
    reason: verdict.unavailable,
  }
}

type Verdict =
  | {
      body: ReadableStream<Uint8Array> | Uint8Array | null
      errorCode?: string
      complete?: Uint8Array
    }
  | 'waking'
  | 'overloaded'
  | { unavailable: UnavailableReason }

async function judge(upstream: Response, options: RelayOptions): Promise<Verdict> {
  const { status, headers } = upstream
  const contentType = headers.get('content-type')
  // Cloudflare's own error pages from Render's zone (52x, 530): never the API.
  // A 524 means the API took the request and had not answered in time.
  if (status >= 520 && status <= 530) {
    return {
      unavailable: status === ORIGIN_TIMEOUT_STATUS ? 'origin_timeout' : 'platform_error',
    }
  }
  // Render's routing layer while a free instance spins up.
  if ((headers.get('x-render-routing') ?? '').includes('hibernate')) return 'waking'
  if (status === 304) return { body: null }
  if (status >= 200 && status < 300) {
    if (isJsonContentType(contentType)) {
      if (options.bufferUpTo === undefined) return { body: upstream.body }
      try {
        const read = await readUpTo(upstream.body, options.bufferUpTo)
        return read.complete
          ? { body: read.bytes, complete: read.bytes }
          : { body: read.stream }
      } catch {
        return { unavailable: 'broken_response' } // The connection dropped mid-body.
      }
    }
    // An HTML 200 for an API path is a platform loading page.
    return isHtmlContentType(contentType) ? 'waking' : { unavailable: 'platform_error' }
  }
  if (status >= 400 && status < 600 && isJsonContentType(contentType)) {
    let bytes: Uint8Array | null
    try {
      bytes = await readCapped(upstream.body, MAX_ERROR_BODY_BYTES)
    } catch {
      return { unavailable: 'broken_response' } // The connection dropped mid-body.
    }
    const code = bytes === null ? null : errorEnvelopeCode(bytes)
    if (bytes !== null && code !== null) return { body: bytes, errorCode: code }
  }
  if (status === 503) return (await isUvicornShed(upstream)) ? 'overloaded' : 'waking'
  // Redirects (never followed) and non-API errors.
  return { unavailable: 'platform_error' }
}

/**
 * uvicorn's own 503 when its connection limit is reached: `text/plain`,
 * exactly "Service Unavailable", no envelope, no Retry-After. The API is up
 * but saturated, which is `overloaded`, not a cold start. (Render's
 * hibernation answer carries `x-render-routing` and is judged before this.)
 */
async function isUvicornShed(upstream: Response): Promise<boolean> {
  if (mediaTypeOf(upstream.headers.get('content-type')) !== 'text/plain') return false
  try {
    const bytes = await readCapped(upstream.body, 64)
    return bytes !== null && new TextDecoder().decode(bytes).trim() === UVICORN_SHED_BODY
  } catch {
    return false
  }
}

function mediaTypeOf(value: string | null): string {
  return (value ?? '').split(';', 1)[0]!.trim().toLowerCase()
}

function passThrough(
  upstream: Response,
  body: ReadableStream<Uint8Array> | Uint8Array | null,
  requestId: string,
): Response {
  const headers = new Headers()
  for (const name of RELAYED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name)
    if (value !== null) headers.set(name, value)
  }
  if (!headers.has('x-request-id')) headers.set('X-Request-ID', requestId)
  applySecurityHeaders(headers)
  return new Response(body, { status: upstream.status, headers })
}

/** 503 `service_waking`: the free origin is spinning up. */
export function serviceWaking(requestId: string): Response {
  return errorResponse(
    503,
    'service_waking',
    'The analysis service is starting up after being idle. Try again shortly.',
    requestId,
    { retryAfterSeconds: RETRY_AFTER_WAKING_S },
  )
}

/** 503 `overloaded`: the origin's server shed the connection before the API ran. */
export function originOverloaded(requestId: string): Response {
  return errorResponse(
    503,
    'overloaded',
    'The analysis service is busy. Try again shortly.',
    requestId,
    { retryAfterSeconds: RETRY_AFTER_OVERLOADED_S, details: { scope: 'global' } },
  )
}

/** 502 (or 503 while the breaker is open) `upstream_unavailable`, saying why. */
export function upstreamUnavailable(
  requestId: string,
  status: 502 | 503,
  retryAfterSeconds: number,
  reason: UnavailableReason,
): Response {
  return errorResponse(
    status,
    'upstream_unavailable',
    'The analysis service is temporarily unreachable. Try again shortly.',
    requestId,
    { retryAfterSeconds, details: { reason } },
  )
}

/** The `error.code` of an API error envelope, or null when `bytes` are not one. */
export function errorEnvelopeCode(bytes: Uint8Array): string | null {
  let payload: unknown
  try {
    payload = JSON.parse(
      new TextDecoder('utf-8', { fatal: true, ignoreBOM: false }).decode(bytes),
    )
  } catch {
    return null
  }
  if (typeof payload !== 'object' || payload === null) return null
  const error = (payload as { error?: unknown }).error
  if (typeof error !== 'object' || error === null) return null
  const { code, message } = error as { code?: unknown; message?: unknown }
  return typeof code === 'string' && typeof message === 'string' ? code : null
}
