/**
 * The single HTTP boundary between the frontend and the analysis API.
 *
 * Every API request goes through `apiRequest`: one place for the base URL,
 * the /api/v1 prefix, JSON handling, protocol headers, and the translation of
 * failures into two error types the UI presents differently:
 *
 * - `ApiError` — the service answered with an error status. Usually that is
 *   the API's structured envelope (an analytical or validation problem, or an
 *   operational state such as rate limiting or a paused quota). An error page
 *   that is not the API's JSON — from the edge, a proxy, or the host — gets a
 *   synthesized code so it can still be presented honestly: `edge_daily_limit`
 *   for a 429, `upstream_unavailable` for a 5xx, `malformed_response` otherwise.
 * - `NetworkError` — no usable answer: the service could not be reached, did
 *   not answer within the caller's time limit, or sent a success status whose
 *   body is not JSON.
 *
 * Retries, the static snapshot and cold-start handling sit one layer up
 * (`resilience.ts`, `snapshot.ts`, `endpoints.ts`).
 */
import { resolveApiBase } from './apiBase'
import { noteServedDataVersion } from './serviceStatus'

export const API_VERSION_PREFIX = '/api/v1'

/** '' (same origin) unless the build set VITE_API_BASE_URL to an absolute URL. */
export function apiBaseUrl(): string {
  return resolveApiBase(import.meta.env.VITE_API_BASE_URL as string | undefined)
}

/** Longest wait the client will honour from a Retry-After value (31 days). */
const MAX_RETRY_AFTER_SECONDS = 31 * 24 * 60 * 60

/** An error status from the service, with the API envelope's fields when it sent one. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: unknown
  readonly requestId: string | null
  /** Seconds the service asked the client to wait (Retry-After, else details), if any. */
  readonly retryAfterSeconds: number | null

  constructor(
    status: number,
    code: string,
    message: string,
    details: unknown,
    requestId: string | null,
    retryAfterSeconds: number | null = null,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
    this.requestId = requestId
    this.retryAfterSeconds = retryAfterSeconds
  }
}

/** The API could not be reached, did not answer in time, or did not speak JSON. */
export class NetworkError extends Error {
  /** The client stopped waiting (`timeoutMs`); the request may still be running. */
  readonly timedOut: boolean
  /**
   * The browser was offline (`navigator.onLine` false) when the request
   * failed: the only failure a reconnect is allowed to retry (`queries.ts`).
   */
  readonly offline: boolean

  constructor(
    message: string,
    options?: { cause?: unknown; timedOut?: boolean; offline?: boolean },
  ) {
    super(message, options)
    this.name = 'NetworkError'
    this.timedOut = options?.timedOut ?? false
    this.offline = options?.offline ?? false
  }
}

/** The browser reports being offline (its 'offline' event sets this). */
export function browserOffline(): boolean {
  return typeof navigator !== 'undefined' && navigator.onLine === false
}

export interface ApiResult<T> {
  data: T
  /** X-Analysis-Key: deterministic identifier of an analysis (analysis endpoints only). */
  analysisKey: string | null
  /** X-Cache: hit | miss | bypass | join | edge, or `snapshot` for a static snapshot file. */
  cache: string | null
  /** X-Data-Version: the data release that produced this response, when reported. */
  dataVersion: string | null
}

interface RequestOptions {
  method?: 'GET' | 'POST'
  /**
   * The request body, already serialized: exactly the bytes to send. Analysis
   * specs are serialized with `stableStringify`, so one spec is always one
   * byte sequence (the edge cache and the static snapshot key on those bytes).
   */
  jsonBody?: string
  signal?: AbortSignal
  /** Prefix the path with /api/v1 (default true; false only for /health). */
  versioned?: boolean
  /** Give up after this many milliseconds with a `NetworkError` (`timedOut`). */
  timeoutMs?: number
  /** Fetch cache mode; `no-cache` revalidates instead of trusting the HTTP cache. */
  cache?: RequestCache
}

/**
 * Retry-After as whole seconds: delta-seconds or an HTTP date. Null when the
 * value is absent or unreadable; capped at `MAX_RETRY_AFTER_SECONDS`.
 */
export function parseRetryAfter(value: string | null, now: number = Date.now()): number | null {
  if (value === null) return null
  const trimmed = value.trim()
  if (/^\d+$/.test(trimmed)) return Math.min(Number(trimmed), MAX_RETRY_AFTER_SECONDS)
  const date = Date.parse(trimmed)
  if (Number.isNaN(date)) return null
  return Math.min(Math.max(0, Math.ceil((date - now) / 1000)), MAX_RETRY_AFTER_SECONDS)
}

/** `details.retry_after_seconds` from the envelope (used when the header is absent or unexposed). */
function detailsRetryAfter(details: unknown): number | null {
  const value = (details as { retry_after_seconds?: unknown } | null)?.retry_after_seconds
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return null
  return Math.min(Math.ceil(value), MAX_RETRY_AFTER_SECONDS)
}

/** An error status whose body is not the API's JSON envelope (edge, proxy or host page). */
function nonJsonError(
  status: number,
  requestId: string | null,
  retryAfter: number | null,
): ApiError {
  if (status === 429) {
    return new ApiError(
      429,
      'edge_daily_limit',
      'The site answered 429 with a page that is not the API: its daily request allowance is used up.',
      null,
      requestId,
      retryAfter,
    )
  }
  if (status >= 500) {
    return new ApiError(
      status,
      'upstream_unavailable',
      `The analysis service's host answered HTTP ${status} with a page that is not the API.`,
      null,
      requestId,
      retryAfter,
    )
  }
  return new ApiError(
    status,
    'malformed_response',
    `The API returned an unreadable ${status} response.`,
    null,
    requestId,
    retryAfter,
  )
}

async function parseErrorEnvelope(response: Response): Promise<ApiError> {
  const requestId = response.headers.get('X-Request-ID')
  const headerRetryAfter = parseRetryAfter(response.headers.get('Retry-After'))
  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    return nonJsonError(response.status, requestId, headerRetryAfter)
  }
  const envelope = payload as { error?: { code?: string; message?: string; details?: unknown } }
  if (envelope && envelope.error && typeof envelope.error.message === 'string') {
    const details = envelope.error.details ?? null
    return new ApiError(
      response.status,
      envelope.error.code ?? 'unknown_error',
      envelope.error.message,
      details,
      requestId,
      headerRetryAfter ?? detailsRetryAfter(details),
    )
  }
  return new ApiError(
    response.status,
    'malformed_response',
    `The API returned an unexpected ${response.status} response.`,
    null,
    requestId,
    headerRetryAfter,
  )
}

function isAbortError(error: unknown): boolean {
  return (error as { name?: unknown } | null)?.name === 'AbortError'
}

/** Combines the caller's abort signal with an optional client-side time limit. */
function withDeadline(signal: AbortSignal | undefined, timeoutMs: number | undefined) {
  if (timeoutMs === undefined) {
    return { signal, timedOut: () => false, clear: () => {} }
  }
  const controller = new AbortController()
  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, timeoutMs)
  const forward = () => controller.abort()
  if (signal?.aborted) controller.abort()
  else signal?.addEventListener('abort', forward, { once: true })
  return {
    signal: controller.signal,
    timedOut: () => timedOut,
    clear: () => {
      clearTimeout(timer)
      signal?.removeEventListener('abort', forward)
    },
  }
}

export async function apiRequest<T>(
  path: string,
  options: RequestOptions = {},
): Promise<ApiResult<T>> {
  const { method = 'GET', jsonBody, signal, versioned = true, timeoutMs, cache } = options
  const url = apiBaseUrl() + (versioned ? API_VERSION_PREFIX : '') + path
  const deadline = withDeadline(signal, timeoutMs)

  // A failure while waiting: the caller's own abort passes through untouched;
  // anything else (including our time limit) is a NetworkError.
  const failure = (cause: unknown, message: string): unknown => {
    if (deadline.timedOut()) {
      return new NetworkError('The analysis service did not answer in time.', {
        cause,
        timedOut: true,
      })
    }
    if (isAbortError(cause)) return cause
    return new NetworkError(message, { cause, offline: browserOffline() })
  }

  try {
    let response: Response
    try {
      response = await fetch(url, {
        method,
        signal: deadline.signal,
        cache,
        headers: jsonBody !== undefined ? { 'Content-Type': 'application/json' } : undefined,
        body: jsonBody,
      })
    } catch (cause) {
      throw failure(cause, 'The analysis service could not be reached.')
    }

    const dataVersion = response.headers.get('X-Data-Version')
    noteServedDataVersion(dataVersion)

    if (!response.ok) {
      throw await parseErrorEnvelope(response)
    }

    let data: T
    try {
      data = (await response.json()) as T
    } catch (cause) {
      throw failure(cause, 'The API returned a response that could not be read as JSON.')
    }

    return {
      data,
      analysisKey: response.headers.get('X-Analysis-Key'),
      cache: response.headers.get('X-Cache'),
      dataVersion,
    }
  } finally {
    deadline.clear()
  }
}
