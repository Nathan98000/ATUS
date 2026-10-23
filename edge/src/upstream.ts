/**
 * The Worker's only way to reach the origin (ADR-011 "Request flow", step 3.6).
 *
 * - No timeout of its own: a cold Render spin-up plus a worst-case query can
 *   take well over a minute, and the API bounds its own work. Nothing here
 *   aborts the call when the client disconnects, but Cloudflare may cancel
 *   the request's pending work then ("tasks associated with that request may
 *   be canceled"); `waitUntil` work gets up to 30 s more. The Worker queues
 *   an analysis's settle before calling (index.ts), and the governor charges
 *   one that is lost anyway when its lease expires.
 * - Exactly one retry, and only when the connection fails before any
 *   response headers arrive (`fetch` rejects). Every routed call is an
 *   idempotent read, and any response — even a 5xx — is final.
 * - A per-isolate circuit breaker: after `BREAKER_THRESHOLD` consecutive
 *   connection failures the origin is skipped for `BREAKER_OPEN_MS`, so a
 *   sleeping or suspended origin is not hammered. Any response closes it.
 */

import { BREAKER_OPEN_MS, BREAKER_THRESHOLD } from './constants'

export class CircuitBreaker {
  private failures = 0
  private openUntil = 0
  private readonly threshold: number
  private readonly openMs: number

  constructor(threshold = BREAKER_THRESHOLD, openMs = BREAKER_OPEN_MS) {
    this.threshold = threshold
    this.openMs = openMs
  }

  /** Milliseconds the origin is still skipped for; 0 when calls may go through. */
  remainingOpenMs(now: number): number {
    return Math.max(0, this.openUntil - now)
  }

  recordFailure(now: number): void {
    this.failures += 1
    if (this.failures >= this.threshold) this.openUntil = now + this.openMs
  }

  recordSuccess(): void {
    this.failures = 0
    this.openUntil = 0
  }
}

/** Module state lives as long as the isolate: this is the per-isolate breaker. */
export const originBreaker = new CircuitBreaker()

export type OriginOutcome =
  | { kind: 'response'; response: Response; attempts: number }
  | { kind: 'circuit-open'; retryAfterMs: number }
  | { kind: 'connect-failed'; attempts: number; error: string }

/**
 * `fetch` the origin with the retry and breaker rules above. Never rejects.
 * `init.body` must be replayable (bytes, not a stream) for the retry.
 */
export async function fetchOrigin(
  url: string,
  init: RequestInit,
  breaker: CircuitBreaker = originBreaker,
): Promise<OriginOutcome> {
  const remaining = breaker.remainingOpenMs(Date.now())
  if (remaining > 0) return { kind: 'circuit-open', retryAfterMs: remaining }
  let error = 'Error'
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    try {
      const response = await fetch(url, init)
      breaker.recordSuccess()
      return { kind: 'response', response, attempts: attempt }
    } catch (err) {
      breaker.recordFailure(Date.now())
      // The name only: runtime messages can carry the (unpublished) origin URL.
      error = err instanceof Error ? err.name : 'Error'
    }
  }
  return { kind: 'connect-failed', attempts: 2, error }
}

/**
 * The origin URL for `path` (which starts with "/") plus a raw `search`
 * string, or `null` when `RENDER_ORIGIN` is unusable. Plain HTTP is accepted
 * only for loopback origins (local development and tests), because every
 * call carries the edge secret.
 */
export function originUrl(
  origin: string | undefined,
  path: string,
  search: string,
): string | null {
  if (!origin) return null
  let base: URL
  try {
    base = new URL(origin)
  } catch {
    return null
  }
  const loopback = ['localhost', '127.0.0.1', '[::1]'].includes(base.hostname)
  if (base.protocol !== 'https:' && !(base.protocol === 'http:' && loopback)) return null
  if (base.username || base.password) return null
  return base.origin + path + search
}
