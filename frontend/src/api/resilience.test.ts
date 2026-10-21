/**
 * The time-budgeted retry policy (audit F12, F64): what is retried, how long
 * it waits, and when it gives up. Attempts are plain functions here; the
 * page-level tests exercise the same loop through MSW.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, NetworkError } from './client'
import { COLD_START_BUDGET_MS, classifyFailure, retryDelayMs, withRetries } from './resilience'
import { isServiceWaking } from './serviceStatus'

function apiError(
  status: number,
  code: string,
  retryAfter: number | null = null,
  details: unknown = null,
): ApiError {
  return new ApiError(status, code, code, details, null, retryAfter)
}

/** An attempt function that fails with `failures` in order, then returns 'ok'. */
function scripted(failures: unknown[]) {
  const calls: number[] = []
  const attempt = async () => {
    calls.push(Date.now())
    const next = failures.shift()
    if (next !== undefined) throw next
    return 'ok'
  }
  return { attempt, calls }
}

describe('classifyFailure', () => {
  it.each([
    [new NetworkError('down'), 'waking'],
    [apiError(503, 'service_waking', 5), 'waking'],
    [apiError(502, 'upstream_unavailable'), 'waking'],
    [apiError(502, 'upstream_unavailable', 10, { reason: 'connect_failed' }), 'waking'],
    [apiError(503, 'upstream_unavailable', 30, { reason: 'circuit_open' }), 'waking'],
    [apiError(502, 'upstream_unavailable', 10, { reason: 'platform_error' }), 'waking'],
    // The API may have taken these: retried once (joins or hits its cache), not for 150 s.
    [apiError(502, 'upstream_unavailable', 10, { reason: 'origin_timeout' }), 'transient'],
    [apiError(502, 'upstream_unavailable', 10, { reason: 'broken_response' }), 'transient'],
    [apiError(429, 'rate_limited', 30000, { scope: 'client', budget: 'prefix' }), 'final'],
    [apiError(503, 'something_new'), 'transient'],
    [apiError(504, 'malformed_response'), 'waking'],
    [apiError(524, 'malformed_response'), 'waking'],
    [apiError(503, 'overloaded', 2), 'transient'],
    [apiError(503, 'database_unavailable', 2), 'transient'],
    [apiError(429, 'rate_limited', 10), 'rate_limited'],
    [apiError(429, 'rate_limited'), 'final'],
    [apiError(429, 'edge_daily_limit'), 'final'],
    [apiError(503, 'database_paused'), 'final'],
    [apiError(503, 'capacity_paused'), 'final'],
    [apiError(503, 'api_paused'), 'final'],
    [apiError(503, 'daily_capacity_reached', 3600), 'final'],
    [apiError(503, 'capacity_unavailable'), 'final'],
    [apiError(503, 'analysis_timeout'), 'final'],
    [apiError(503, 'data_version_mismatch', 30), 'final'],
    [apiError(413, 'payload_too_large'), 'final'],
    [apiError(403, 'forbidden_origin'), 'final'],
    [apiError(404, 'not_found'), 'final'],
    [apiError(422, 'invalid_spec'), 'final'],
    [apiError(500, 'internal_error'), 'final'],
    [new Error('bug'), 'final'],
  ])('%s → %s', (error, expected) => {
    expect(classifyFailure(error)).toBe(expected)
  })

  it('does not treat being offline as a cold start', () => {
    vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false)
    expect(classifyFailure(new NetworkError('offline'))).toBe('final')
  })
})

describe('retryDelayMs', () => {
  it('honours Retry-After (at least one second)', () => {
    expect(retryDelayMs(apiError(503, 'service_waking', 5), 1)).toBe(5000)
    expect(retryDelayMs(apiError(503, 'service_waking', 0), 1)).toBe(1000)
  })

  it('backs off exponentially, capped at 8 s, without Retry-After', () => {
    const delays = [1, 2, 3, 4, 5, 6].map((n) => retryDelayMs(new NetworkError('x'), n))
    expect(delays).toEqual([1000, 2000, 4000, 8000, 8000, 8000])
  })
})

describe('withRetries', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('keeps a metadata call alive through a cold start, honouring Retry-After', async () => {
    const { attempt, calls } = scripted([
      apiError(503, 'service_waking', 5),
      apiError(503, 'service_waking', 5),
      new NetworkError('reset'),
    ])
    const result = withRetries(attempt, { kind: 'metadata' })
    await vi.advanceTimersByTimeAsync(5000 + 5000 + 4000)
    await expect(result).resolves.toBe('ok')
    const gaps = calls.slice(1).map((time, index) => time - (calls[index] as number))
    // Retry-After twice, then backoff for the third attempt (no header): 4 s.
    expect(gaps).toEqual([5000, 5000, 4000])
  })

  it('shows the waking state while it waits for a waking host', async () => {
    const { attempt } = scripted([apiError(503, 'service_waking', 5)])
    const result = withRetries(attempt, { kind: 'analysis' })
    await vi.advanceTimersByTimeAsync(0)
    expect(isServiceWaking()).toBe(true)
    await vi.advanceTimersByTimeAsync(5000)
    await expect(result).resolves.toBe('ok')
    expect(isServiceWaking()).toBe(false)
  })

  it('gives up with the last error once the budget would be exceeded', async () => {
    const attempt = vi.fn(async () => {
      throw apiError(503, 'service_waking', 5)
    })
    const result = withRetries(attempt, { kind: 'metadata' })
    const settled = result.catch((error: unknown) => error)
    await vi.advanceTimersByTimeAsync(COLD_START_BUDGET_MS + 10_000)
    const error = (await settled) as ApiError
    expect(error.code).toBe('service_waking')
    // 150 s budget / 5 s Retry-After: the first attempt plus 30 retries.
    expect(attempt).toHaveBeenCalledTimes(31)
  })

  it('never retries a final answer', async () => {
    for (const failure of [
      apiError(422, 'invalid_spec'),
      apiError(404, 'not_found'),
      apiError(503, 'database_paused'),
      apiError(429, 'edge_daily_limit'),
    ]) {
      const { attempt, calls } = scripted([failure])
      await expect(withRetries(attempt, { kind: 'metadata' })).rejects.toBe(failure)
      expect(calls).toHaveLength(1)
    }
  })

  it('retries a metadata 429 only when it carries Retry-After', async () => {
    const { attempt, calls } = scripted([apiError(429, 'rate_limited', 3)])
    const result = withRetries(attempt, { kind: 'metadata' })
    await vi.advanceTimersByTimeAsync(3000)
    await expect(result).resolves.toBe('ok')
    expect(calls).toHaveLength(2)
  })

  it('retries an analysis once on a transient 503, honouring Retry-After (F64)', async () => {
    const { attempt, calls } = scripted([apiError(503, 'overloaded', 2)])
    const result = withRetries(attempt, { kind: 'analysis' })
    await vi.advanceTimersByTimeAsync(2000)
    await expect(result).resolves.toBe('ok')
    expect(calls).toHaveLength(2)
  })

  it('re-sends an analysis the API may have computed only once (a 524 at the edge)', async () => {
    // A cold start keeps retrying; an origin timeout does not: the API took
    // the request, so one retry joins its computation or hits its cache.
    const timeout = apiError(502, 'upstream_unavailable', 10, { reason: 'origin_timeout' })
    const { attempt, calls } = scripted([timeout, timeout])
    const result = withRetries(attempt, { kind: 'analysis' }).catch((error: unknown) => error)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(await result).toBe(timeout)
    expect(calls).toHaveLength(2)
    // ...and the waking notice is not shown for it.
    expect(isServiceWaking()).toBe(false)
  })

  it('keeps retrying an analysis that never reached the API', async () => {
    const refused = apiError(502, 'upstream_unavailable', 10, { reason: 'connect_failed' })
    const { attempt, calls } = scripted([refused, refused, refused])
    const result = withRetries(attempt, { kind: 'analysis' })
    await vi.advanceTimersByTimeAsync(30_000)
    await expect(result).resolves.toBe('ok')
    expect(calls).toHaveLength(4)
  })

  it('never retries the per-network daily allowance, even for metadata', async () => {
    const denied = apiError(429, 'rate_limited', 3600, { scope: 'client', budget: 'prefix' })
    const { attempt, calls } = scripted([denied])
    await expect(withRetries(attempt, { kind: 'metadata' })).rejects.toBe(denied)
    expect(calls).toHaveLength(1)
  })

  it('does not retry an analysis a second time on a transient 503', async () => {
    const second = apiError(503, 'database_unavailable', 1)
    const { attempt, calls } = scripted([apiError(503, 'overloaded', 1), second])
    const result = withRetries(attempt, { kind: 'analysis' }).catch((error: unknown) => error)
    await vi.advanceTimersByTimeAsync(5000)
    expect(await result).toBe(second)
    expect(calls).toHaveLength(2)
  })

  it('never retries an analysis rate limit or a client time-out', async () => {
    for (const failure of [
      apiError(429, 'rate_limited', 5),
      new NetworkError('slow', { timedOut: true }),
    ]) {
      const { attempt, calls } = scripted([failure])
      await expect(withRetries(attempt, { kind: 'analysis' })).rejects.toBe(failure)
      expect(calls).toHaveLength(1)
    }
  })

  it('stops waiting when the caller aborts', async () => {
    const controller = new AbortController()
    const { attempt, calls } = scripted([apiError(503, 'service_waking', 30)])
    const result = withRetries(attempt, { kind: 'metadata', signal: controller.signal }).catch(
      (error: unknown) => error,
    )
    await vi.advanceTimersByTimeAsync(1000)
    controller.abort()
    expect(await result).toMatchObject({ name: 'AbortError' })
    await vi.advanceTimersByTimeAsync(60_000)
    expect(calls).toHaveLength(1)
    expect(isServiceWaking()).toBe(false)
  })
})
