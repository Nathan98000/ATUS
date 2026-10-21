/**
 * Audit F13: every error code of the v1 contract, the edge's codes and the
 * client-synthesized ones get a specific, public-facing presentation.
 */
import { describe, expect, it } from 'vitest'

import { readFileSync } from 'node:fs'

import { ApiError, NetworkError } from '../api/client'
import { DataNotLoadedError, IncompatibleApiError } from '../api/compat'
import { HEAVY_YEAR_QUERIES, isFullPageError, presentError } from './presentError'

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

describe('presentError: review round 1', () => {
  const now = Date.UTC(2026, 9, 21, 12, 0, 0)

  it('database_paused names no reset date: its fixed Retry-After (3600 s) is not the reset', () => {
    // The API always sends Retry-After 3600 with database_paused (errors.py);
    // the provider's allowance resets at its next billing period, not in an hour.
    const presented = presentError(
      apiError(503, 'database_paused', {
        retryAfter: 3600,
        details: { scope: 'monthly', retry_after_seconds: 3600 },
      }),
      now,
    )
    expect(presented.message).not.toMatch(/around/)
    expect(presented.message).toMatch(/when the allowance resets\./)
  })

  it('database_paused names the date only when the service says when the pause ends', () => {
    const presented = presentError(
      apiError(503, 'database_paused', {
        retryAfter: 3600,
        details: { scope: 'monthly', resumes_at: '2026-11-05T00:00:00Z' },
      }),
      now,
    )
    const date = new Date(Date.UTC(2026, 10, 5)).toLocaleDateString(undefined, {
      month: 'long',
      day: 'numeric',
    })
    expect(presented.message).toContain(`(around ${date})`)
  })

  it('every retryable presentation carries the Retry-After the service sent', () => {
    for (const [status, code] of [
      [429, 'rate_limited'],
      [503, 'overloaded'],
      [503, 'analysis_timeout'],
      [503, 'capacity_unavailable'],
      [503, 'data_version_mismatch'],
      [503, 'service_waking'],
      [502, 'upstream_unavailable'],
      [503, 'database_unavailable'],
      [503, 'schema_not_initialized'],
      [503, 'data_not_loaded'],
      [500, 'internal_error'],
    ] as const) {
      const presented = presentError(apiError(status, code, { retryAfter: 7 }))
      expect(presented.retryable, code).toBe(true)
      expect(presented.retryAfterSeconds, code).toBe(7)
    }
  })

  it('invalid_analysis (a 422 domain rejection) is a request problem in the API’s words', () => {
    const presented = presentError(apiError(422, 'invalid_analysis'))
    expect(presented.kind).toBe('request')
    expect(presented.message).toBe('server message for invalid_analysis')
    expect(presented.adjustable).toBe(true)
    expect(presented.retryable).toBe(false)
    expect(presented.message).not.toMatch(/not in your request/)
    expect(presented.heading).not.toBe(GENERIC_HEADING)
  })

  it('validation details in the API’s own shape ({location, message}) become field lines', () => {
    const presented = presentError(
      apiError(422, 'validation_error', {
        details: {
          errors: [
            {
              location: ['body', 'measure'],
              message: "Input should be 'average_minutes_per_day' or 'participation_rate'",
              type: 'literal_error',
            },
            { location: ['body', 'population', 'age_min'], message: 'Too small', type: 'x' },
          ],
        },
      }),
    )
    expect(presented.detailLines).toEqual([
      "measure: Input should be 'average_minutes_per_day' or 'participation_rate'",
      'population.age_min: Too small',
    ])
    // FastAPI's default shape ({loc, msg}) still reads.
    expect(
      presentError(
        apiError(422, 'validation_error', {
          details: { errors: [{ loc: ['query', 'level'], msg: 'Too big' }] },
        }),
      ).detailLines,
    ).toEqual(['query.level: Too big'])
  })

  it('upstream_unavailable says what happened, from details.reason', () => {
    const present = (reason: string | null) =>
      presentError(
        apiError(502, 'upstream_unavailable', { details: reason === null ? null : { reason } }),
      )
    // Nothing answered: no hosting provider page was involved.
    for (const reason of ['connect_failed', 'circuit_open']) {
      const presented = present(reason)
      expect(presented.heading, reason).toBe('The analysis service could not be reached')
      expect(presented.message, reason).not.toMatch(/answered/)
    }
    expect(present('not_configured').message).toMatch(/not set up/)
    expect(present('not_configured').message).not.toMatch(/answered/)
    // The API took the request: say so, not "the hosting provider answered".
    expect(present('origin_timeout').heading).toBe(
      'The analysis service did not answer in time',
    )
    expect(present('broken_response').message).toMatch(/broke off/)
    // A platform page, or a non-JSON 5xx the client mapped itself.
    for (const reason of ['platform_error', null]) {
      expect(present(reason).message, String(reason)).toMatch(/hosting provider answered/)
    }
  })

  it('a host that stays down past the cold-start budget is not promised to be "temporary"', () => {
    // These are shown only after the 150 s budget of automatic retries ran
    // out; free hosting can also be suspended for the rest of the month.
    const cases = [
      apiError(503, 'service_waking', { retryAfter: 5 }),
      apiError(502, 'upstream_unavailable', { details: { reason: 'connect_failed' } }),
      apiError(503, 'upstream_unavailable', { details: { reason: 'circuit_open' } }),
      apiError(502, 'upstream_unavailable', { details: { reason: 'platform_error' } }),
      apiError(503, 'upstream_unavailable'),
    ]
    for (const error of cases) {
      const presented = presentError(error)
      expect(presented.message, error.code).not.toMatch(/in a moment|usually temporary/)
      expect(presented.message, error.code).toMatch(/more than 2 minutes/)
      expect(presented.hint, error.code).toMatch(/paused/)
      expect(presented.hint, error.code).toMatch(/Starter examples/)
      expect(presented.kind).toBe('service')
    }
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

describe('presentError: review round 2', () => {
  const now = Date.UTC(2026, 9, 5, 12, 0, 0)

  it('the heavy-only daily cap says that shorter analyses still run, and offers to adjust', () => {
    // The governor refuses only heavy analyses (8 or more year queries) once
    // 300 were admitted in the UTC day; light ones are still admitted.
    const presented = presentError(
      apiError(503, 'daily_capacity_reached', {
        retryAfter: 12 * 3600,
        details: { scope: 'daily', budget: 'heavy', resumes_at: '2026-10-06T00:00:00Z' },
      }),
      now,
    )
    expect(presented.kind).toBe('limit')
    expect(presented.heading).toBe('Today’s capacity for long analyses is used up')
    expect(presented.heading).not.toMatch(/new analyses/)
    expect(presented.message).toMatch(/8 or more years \(4 or more for a comparison\)/)
    expect(presented.message).toMatch(
      /Analyses of up to 7 years \(up to 3 for a comparison\) still run/,
    )
    expect(presented.message).toMatch(/00:00 UTC \(.+ your time\)/)
    expect(presented.adjustable).toBe(true)
    expect(presented.retryable).toBe(false)
  })

  it('the heavy threshold is the edge governor’s', () => {
    const constants = readFileSync('../edge/src/constants.ts', 'utf8')
    expect(constants).toContain(`export const HEAVY_YEAR_QUERIES = ${HEAVY_YEAR_QUERIES}\n`)
  })

  it('a monthly pacing pause of hours names the time it lifts, not a monthly reset', () => {
    const presented = presentError(
      apiError(503, 'capacity_paused', {
        retryAfter: 7200,
        details: {
          scope: 'monthly',
          budget: 'cu_month',
          resumes_at: new Date(now + 7200_000).toISOString(),
        },
      }),
      now,
    )
    expect(presented.kind).toBe('paused')
    expect(presented.message).not.toMatch(/nearly used up/)
    expect(presented.message).not.toMatch(/until it resets/)
    expect(presented.message).toMatch(/spread/)
    expect(presented.message).toMatch(/resume (tomorrow )?at about \d{1,2}:\d\d/)
  })

  it('a pause at the monthly compute cap names the day it lifts', () => {
    const presented = presentError(
      apiError(503, 'capacity_paused', {
        retryAfter: 20 * 24 * 3600,
        details: {
          scope: 'monthly',
          budget: 'cu_month',
          resumes_at: new Date(now + 20 * 24 * 3600_000).toISOString(),
        },
      }),
      now,
    )
    expect(presented.message).toMatch(/resume around October 25/)
    expect(presented.message).not.toMatch(/nearly used up/)
  })

  it('the egress budget keeps the "nearly used up until it resets" copy', () => {
    const presented = presentError(
      apiError(503, 'capacity_paused', {
        details: {
          scope: 'monthly',
          budget: 'egress',
          resumes_at: new Date(now + 20 * 24 * 3600_000).toISOString(),
        },
      }),
      now,
    )
    expect(presented.message).toMatch(/nearly used up/)
    expect(presented.message).toMatch(/until it resets \(around October 25\)/)
  })

  it('a v1 /meta in a shape this page cannot read does not claim "v1, but v1"', () => {
    const presented = presentError(
      new IncompatibleApiError('The service metadata does not have the shape…', 'v1'),
    )
    expect(presented.kind).toBe('incompatible')
    expect(presented.message).not.toMatch(/reported version v1/)
    expect(presented.message).toMatch(/metadata is not in the form this page reads/)
    expect(presented.message).toMatch(/Reloading the page/)
  })

  it('a service without survey data loaded is "no data", with Try again', () => {
    const presented = presentError(new DataNotLoadedError())
    expect(presented.kind).toBe('service')
    expect(presented.heading).toBe('No survey data is loaded')
    expect(presented.retryable).toBe(true)
    expect(isFullPageError(new DataNotLoadedError())).toBe(false)
  })

  it('forbidden_origin through the site is the deployment’s fault, not the visitor’s', () => {
    const presented = presentError(apiError(403, 'forbidden_origin'))
    expect(presented.message).not.toMatch(/only accepts requests sent through/)
    expect(presented.message).toMatch(/problem in ATUS Explorer’s deployment/)
    expect(presented.message).toMatch(/not in your analysis/)
    expect(presented.hint).toMatch(/Starter examples/)
    expect(presented.retryable).toBe(false)
  })
})
