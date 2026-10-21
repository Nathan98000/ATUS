/**
 * Audit F13: every error code of the v1 contract, the edge's codes and the
 * client-synthesized ones get a specific, public-facing presentation.
 */
import { describe, expect, it } from 'vitest'

import { ApiError, NetworkError } from '../api/client'
import { IncompatibleApiError } from '../api/compat'
import { isFullPageError, presentError } from './presentError'

const GENERIC_HEADING = 'The analysis service reported an unexpected problem'

function apiError(
  status: number,
  code: string,
  { retryAfter = null, details = null }: { retryAfter?: number | null; details?: unknown } = {},
) {
  return new ApiError(status, code, `server message for ${code}`, details, 'req-9', retryAfter)
}

describe('presentError: every contract code is specific', () => {
  it.each([
    [429, 'rate_limited', 'limit'],
    [503, 'overloaded', 'service'],
    [503, 'analysis_timeout', 'service'],
    [503, 'daily_capacity_reached', 'limit'],
    [503, 'capacity_paused', 'paused'],
    [503, 'capacity_unavailable', 'service'],
    [503, 'database_paused', 'paused'],
    [503, 'data_version_mismatch', 'service'],
    [413, 'payload_too_large', 'request'],
    [403, 'forbidden_origin', 'app'],
    [503, 'service_waking', 'service'],
    [502, 'upstream_unavailable', 'service'],
    [503, 'data_not_loaded', 'service'],
    [503, 'database_unavailable', 'service'],
    [429, 'edge_daily_limit', 'paused'],
    [404, 'not_found', 'app'],
    [405, 'method_not_allowed', 'app'],
    [415, 'unsupported_media_type', 'app'],
  ])('%i %s → %s', (status, code, kind) => {
    const presented = presentError(apiError(status, code))
    expect(presented.kind).toBe(kind)
    expect(presented.heading).not.toBe(GENERIC_HEADING)
    // Operational states never show the server's internal wording as the message.
    expect(presented.message).not.toContain('server message')
    expect(presented.technical).toBe(`HTTP ${status} · ${code} · request req-9`)
  })

  it('a request outside the HTTP contract says "reload", never "not your request, try again"', () => {
    // The retry policy treats these 4xx answers as final, so the copy must
    // not offer a "Try again" that cannot help or blame the service.
    for (const [status, code] of [
      [404, 'not_found'],
      [405, 'method_not_allowed'],
      [415, 'unsupported_media_type'],
    ] as const) {
      const presented = presentError(apiError(status, code))
      expect(presented.retryable).toBe(false)
      expect(presented.message).not.toMatch(/not in your request/)
      expect(presented.hint).toMatch(/Reload the page/)
    }
  })

  it('an unknown activity code from the lexicon endpoint is a request problem', () => {
    const presented = presentError(apiError(404, 'activity_not_found'))
    expect(presented.kind).toBe('request')
    expect(presented.heading).toBe('Unknown activity')
    expect(presented.message).toBe('server message for activity_not_found')
  })

  it('no longer claims "the service is up" for a database outage', () => {
    const presented = presentError(apiError(503, 'database_unavailable'))
    expect(presented.message).not.toMatch(/service is up/i)
    expect(presented.heading).toMatch(/temporarily unavailable/)
    expect(presented.retryable).toBe(true)
  })

  it('a rate limit says how long to wait and drives a countdown', () => {
    const presented = presentError(apiError(429, 'rate_limited', { retryAfter: 42 }))
    expect(presented.message).toContain('about 42 seconds')
    expect(presented.retryAfterSeconds).toBe(42)
    expect(presented.retryable).toBe(true)
  })

  it('the per-network daily allowance at the edge never says "per minute"', () => {
    // The governor's denial: 429 rate_limited, scope client, budget prefix,
    // Retry-After until 00:00 UTC (up to ~24 h).
    const now = Date.UTC(2026, 9, 20, 15, 0, 0)
    const presented = presentError(
      apiError(429, 'rate_limited', {
        retryAfter: 9 * 3600,
        details: {
          scope: 'client',
          budget: 'prefix',
          resumes_at: '2026-10-21T00:00:00Z',
          retry_after_seconds: 32400,
        },
      }),
      now,
    )
    expect(presented.kind).toBe('limit')
    expect(presented.message).not.toMatch(/per minute/)
    expect(presented.message).toMatch(/per day/)
    expect(presented.message).toMatch(/00:00 UTC \(.+ your time\)/)
    expect(presented.retryable).toBe(false)
    expect(presented.retryAfterSeconds).toBeNull() // no countdown of hours
    expect(presented.hint).toMatch(/Starter examples/)
    expect(
      isFullPageError(apiError(429, 'rate_limited', { details: { budget: 'prefix' } })),
    ).toBe(false)
  })

  it('a long rate-limit wait without a budget names a time, not a countdown', () => {
    const now = Date.UTC(2026, 9, 20, 15, 0, 0)
    const presented = presentError(apiError(429, 'rate_limited', { retryAfter: 2 * 3600 }), now)
    expect(presented.message).not.toMatch(/per minute/)
    expect(presented.message).toMatch(/at about /)
    expect(presented.retryable).toBe(false)
    expect(presented.retryAfterSeconds).toBeNull()
  })

  it('the API and edge per-minute limits keep the countdown', () => {
    for (const details of [null, { scope: 'client', retry_after_seconds: 8 }]) {
      const presented = presentError(apiError(429, 'rate_limited', { retryAfter: 8, details }))
      expect(presented.message).toMatch(/per minute/)
      expect(presented.retryAfterSeconds).toBe(8)
      expect(presented.retryable).toBe(true)
    }
  })

  it('a daily capacity limit is the shared allowance, with the reset time', () => {
    // The scopes the senders use: the API's DailyCapacityReachedError says
    // 'daily'; the edge governor says 'global' (all analyses) or 'daily'
    // (heavy analyses, compute). A per-network daily denial is never this
    // code: it is rate_limited with budget 'prefix' (tested above).
    const now = Date.UTC(2026, 9, 20, 15, 0, 0)
    for (const details of [
      null,
      { scope: 'daily', retry_after_seconds: 32400 },
      { scope: 'global', budget: 'global', resumes_at: '2026-10-21T00:00:00Z' },
      { scope: 'daily', budget: 'heavy', resumes_at: '2026-10-21T00:00:00Z' },
      { scope: 'daily', budget: 'cu_day', resumes_at: '2026-10-21T00:00:00Z' },
    ]) {
      const presented = presentError(
        apiError(503, 'daily_capacity_reached', { retryAfter: 9 * 3600, details }),
        now,
      )
      expect(presented.kind).toBe('limit')
      expect(presented.heading).toBe('Today’s capacity for new analyses is used up')
      expect(presented.message).toMatch(/limited number of new analyses per day/)
      expect(presented.message).toMatch(/today’s are used up/)
      expect(presented.message).toMatch(/00:00 UTC \(.+ your time\)/)
      expect(presented.message).not.toMatch(/Your network/)
      expect(presented.retryable).toBe(false)
      expect(presented.retryAfterSeconds).toBeNull()
      expect(presented.hint).toMatch(/Starter examples/)
    }
  })

  it('a paused quota explains free hosting and what still works', () => {
    const now = Date.UTC(2026, 9, 20, 12, 0, 0)
    const presented = presentError(
      apiError(503, 'capacity_paused', { retryAfter: 10 * 24 * 3600 }),
      now,
    )
    expect(presented.message).toMatch(/free hosting/)
    expect(presented.message).toMatch(/\(around .+\)/)
    expect(presented.hint).toMatch(/Starter examples/)
  })

  it('the edge daily limit is a full-page state with the UTC reset', () => {
    const presented = presentError(apiError(429, 'edge_daily_limit'))
    expect(presented.heading).toMatch(/today’s request limit/)
    expect(presented.message).toMatch(/00:00 UTC/)
    expect(isFullPageError(apiError(429, 'edge_daily_limit'))).toBe(true)
  })

  it('a non-JSON host error page is an unavailability, not a broken request', () => {
    const presented = presentError(apiError(504, 'upstream_unavailable'))
    expect(presented.heading).toBe('The analysis service is unavailable')
    expect(presented.adjustable).toBe(false)
  })

  it('an analysis timeout suggests a smaller analysis', () => {
    const presented = presentError(apiError(503, 'analysis_timeout'))
    expect(presented.adjustable).toBe(true)
    expect(presented.message).toMatch(/fewer years/)
  })

  it('unknown codes still get the generic service presentation', () => {
    const presented = presentError(apiError(500, 'internal_error'))
    expect(presented.heading).toBe(GENERIC_HEADING)
    expect(presented.retryable).toBe(true)
  })
})

describe('presentError: client-side failures', () => {
  it('an incompatible API is a full-page state naming both versions', () => {
    const error = new IncompatibleApiError('The service speaks API v2.', 'v2')
    const presented = presentError(error)
    expect(presented.kind).toBe('incompatible')
    expect(presented.message).toMatch(/API v1/)
    expect(presented.message).toMatch(/version v2/)
    expect(isFullPageError(error)).toBe(true)
  })

  it('distinguishes a time-out from an unreachable service', () => {
    expect(presentError(new NetworkError('x', { timedOut: true })).message).toMatch(
      /did not answer in time/,
    )
    expect(presentError(new NetworkError('x')).message).toMatch(/never got an answer/)
  })

  it('only paused and incompatible states are full-page', () => {
    expect(isFullPageError(apiError(503, 'database_paused'))).toBe(true)
    expect(isFullPageError(apiError(503, 'capacity_paused'))).toBe(true)
    expect(isFullPageError(apiError(503, 'daily_capacity_reached'))).toBe(false)
    expect(isFullPageError(apiError(429, 'rate_limited'))).toBe(false)
    expect(isFullPageError(new NetworkError('x'))).toBe(false)
  })
})
