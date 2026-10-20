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
 * hibernate 503 or HTML loading page, Cloudflare 52x pages, redirects — are
 * replaced by JSON envelopes the SPA understands.
 */

import {
  EDGE_VERSION,
  MAX_ERROR_BODY_BYTES,
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
export type RelayOutcome = 'pass' | 'waking' | 'unavailable'

export interface Relayed {
  response: Response
  outcome: RelayOutcome
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
 * everything else, to 503 `service_waking` (the origin is starting) or 502
 * `upstream_unavailable`.
 */
export async function relayResponse(
  upstream: Response,
  requestId: string,
  options: RelayOptions = {},
): Promise<Relayed> {
  const verdict = await judge(upstream, options)
  if (typeof verdict === 'object') {
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
  return { response: upstreamUnavailable(requestId, 502), outcome: 'unavailable' }
}

type Verdict =
  | {
      body: ReadableStream<Uint8Array> | Uint8Array | null
      errorCode?: string
      complete?: Uint8Array
    }
  | 'waking'
  | 'unavailable'

async function judge(upstream: Response, options: RelayOptions): Promise<Verdict> {
  const { status, headers } = upstream
  const contentType = headers.get('content-type')
  // Cloudflare's own error pages from Render's zone (52x, 530): never the API.
  if (status >= 520 && status <= 530) return 'unavailable'
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
        return 'unavailable' // The connection dropped mid-body.
      }
    }
    // An HTML 200 for an API path is a platform loading page.
    return isHtmlContentType(contentType) ? 'waking' : 'unavailable'
  }
  if (status >= 400 && status < 600 && isJsonContentType(contentType)) {
    let bytes: Uint8Array | null
    try {
      bytes = await readCapped(upstream.body, MAX_ERROR_BODY_BYTES)
    } catch {
      return 'unavailable' // The connection dropped mid-body.
    }
    const code = bytes === null ? null : errorEnvelopeCode(bytes)
    if (bytes !== null && code !== null) return { body: bytes, errorCode: code }
  }
  // Redirects (never followed) and non-API errors.
  return status === 503 ? 'waking' : 'unavailable'
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

/** 502 (or 503 while the breaker is open) `upstream_unavailable`. */
export function upstreamUnavailable(
  requestId: string,
  status: 502 | 503,
  retryAfterSeconds: number = RETRY_AFTER_UNAVAILABLE_S,
): Response {
  return errorResponse(
    status,
    'upstream_unavailable',
    'The analysis service is temporarily unreachable. Try again shortly.',
    requestId,
    { retryAfterSeconds },
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
