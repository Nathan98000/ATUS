/**
 * Time-budgeted retries, so a free-tier cold start is a wait rather than an
 * error page (audit F12, F64).
 *
 * The free API host sleeps when idle and takes about a minute to wake; while
 * it wakes, requests fail fast (the edge answers `service_waking` with
 * `Retry-After`), hang, or come back as a host error page. One logical call
 * therefore spans several attempts:
 *
 * - **Metadata GETs** retry every failure that is not final until
 *   `COLD_START_BUDGET_MS` has passed, honouring `Retry-After`.
 * - **Analysis POSTs** retry "waking" failures (the host was not up, so no
 *   work was done) within the same budget, and other transient failures
 *   (`overloaded`, `database_unavailable`) once. A rate limit or a client
 *   time-out is never retried automatically: the visitor decides.
 * - Nothing retries a final answer: a 4xx other than a 429 with
 *   `Retry-After`, a paused quota, an analysis time-out, a data-version
 *   mismatch, or an incompatible API.
 *
 * TanStack Query's own retries are off for these queries (`retry: false`);
 * this loop replaces them so the budget, the delays and the waking notice
 * are decided in one place.
 */
import { ApiError, NetworkError } from './client'
import { trackApiCall, type CallKind } from './serviceStatus'

/** Total time a call keeps retrying while the service wakes (ADR-011: "within a 150 s budget"). */
export const COLD_START_BUDGET_MS = 150_000

const BACKOFF_BASE_MS = 1_000
const BACKOFF_MAX_MS = 8_000
const MIN_RETRY_AFTER_MS = 1_000

/** How a failed attempt should be treated by the retry policy. */
export type FailureClass = 'waking' | 'transient' | 'rate_limited' | 'final'

/** Answers that retrying cannot change (or that would spend quota to no effect). */
const FINAL_CODES = new Set([
  'analysis_timeout',
  'capacity_paused',
  'capacity_unavailable',
  'daily_capacity_reached',
  'data_not_loaded',
  'data_version_mismatch',
  'database_paused',
  'edge_daily_limit',
  'forbidden_origin',
  'payload_too_large',
  'schema_not_initialized',
])

/** The host is not answering yet: nothing was computed. */
const WAKING_CODES = new Set(['service_waking', 'upstream_unavailable'])

/** The service answered but could not take the request right now. */
const TRANSIENT_CODES = new Set(['overloaded', 'database_unavailable'])

export function classifyFailure(error: unknown): FailureClass {
  if (error instanceof NetworkError) {
    // Offline is not a cold start; say so instead of waiting out the budget.
    if (typeof navigator !== 'undefined' && navigator.onLine === false) return 'final'
    return 'waking'
  }
  if (!(error instanceof ApiError)) return 'final'
  if (FINAL_CODES.has(error.code)) return 'final'
  if (WAKING_CODES.has(error.code)) return 'waking'
  if (TRANSIENT_CODES.has(error.code)) return 'transient'
  if (error.status === 429) {
    return error.code === 'rate_limited' && error.retryAfterSeconds !== null
      ? 'rate_limited'
      : 'final'
  }
  // Gateway failures without a known code (502/503/504, Cloudflare 52x):
  // typical of a host that is still starting.
  if ([502, 503, 504].includes(error.status) || (error.status >= 520 && error.status <= 530)) {
    return 'waking'
  }
  return 'final'
}

/** Delay before attempt `attempt + 1`: Retry-After when given, else capped exponential backoff. */
export function retryDelayMs(error: unknown, attempt: number): number {
  const retryAfter = error instanceof ApiError ? error.retryAfterSeconds : null
  if (retryAfter !== null) return Math.max(MIN_RETRY_AFTER_MS, retryAfter * 1000)
  return Math.min(BACKOFF_BASE_MS * 2 ** (attempt - 1), BACKOFF_MAX_MS)
}

function mayRetry(
  kind: CallKind,
  failure: FailureClass,
  error: unknown,
  transientRetries: number,
): boolean {
  if (failure === 'final') return false
  if (kind === 'metadata') return true
  if (failure === 'waking') return !(error instanceof NetworkError && error.timedOut)
  if (failure === 'transient') return transientRetries < 1
  return false
}

/** Shown as "waking": the host is not up yet, or its database is resuming. */
function looksLikeWaking(failure: FailureClass, error: unknown): boolean {
  return (
    failure === 'waking' || (error instanceof ApiError && error.code === 'database_unavailable')
  )
}

function isAbortError(error: unknown): boolean {
  return (error as { name?: unknown } | null)?.name === 'AbortError'
}

function sleep(ms: number, signal: AbortSignal | undefined): Promise<void> {
  return new Promise((resolve, reject) => {
    const aborted = () => new DOMException('The operation was aborted.', 'AbortError')
    if (signal?.aborted) {
      reject(aborted())
      return
    }
    const onAbort = () => {
      clearTimeout(timer)
      reject(aborted())
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

export interface RetryOptions {
  kind: CallKind
  /** The query's abort signal: cancels both the attempt and any wait between attempts. */
  signal?: AbortSignal
  budgetMs?: number
}

/**
 * Runs `attempt` until it succeeds, fails finally, or the budget would be
 * exceeded by the next wait; the last error is rethrown unchanged.
 */
export async function withRetries<T>(
  attempt: () => Promise<T>,
  options: RetryOptions,
): Promise<T> {
  const budgetMs = options.budgetMs ?? COLD_START_BUDGET_MS
  const startedAt = Date.now()
  const tracker = trackApiCall(options.kind)
  let transientRetries = 0
  try {
    for (let attemptNumber = 1; ; attemptNumber += 1) {
      try {
        const result = await attempt()
        tracker.answered()
        return result
      } catch (error) {
        if (isAbortError(error)) throw error
        const failure = classifyFailure(error)
        if (error instanceof ApiError && failure !== 'waking') tracker.answered()
        if (!mayRetry(options.kind, failure, error, transientRetries)) throw error
        const delay = retryDelayMs(error, attemptNumber)
        if (Date.now() - startedAt + delay > budgetMs) throw error
        if (failure === 'transient') transientRetries += 1
        tracker.setWaking(looksLikeWaking(failure, error))
        await sleep(delay, options.signal)
      }
    }
  } finally {
    tracker.finish()
  }
}
