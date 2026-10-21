/**
 * The owner's pause switch for the API (review round 2; brief §14;
 * docs/security.md R13).
 *
 * What a suspended Render service answers is undocumented, so the Worker
 * cannot tell a suspension from a cold start: visitors would get the waking
 * notice and its retries. When the owner knows the API is paused (Render
 * suspended it until next month or at its discretion, or for maintenance),
 * two optional Worker secrets say so. They are secrets, not vars, only so
 * that `.github/workflows/deploy.yml` (operation `pause-api` / `resume-api`)
 * can set and clear them with `wrangler secret bulk`, which needs no rebuild
 * of the site, and so that every later `wrangler deploy` keeps them; the
 * values themselves are not secret:
 *
 * - `API_PAUSED_UNTIL`: when the API is expected back, `YYYY-MM-DD` or
 *   `YYYY-MM-DDTHH:MM[:SS]Z` (UTC), or `unknown`. Empty or absent: off. A
 *   time already past: off, so the switch expires by itself. Any other text
 *   pauses without a date (the owner meant to pause).
 * - `API_PAUSED_REASON`: `render_quota`, `render_suspended` or
 *   `maintenance`; anything else is reported as null.
 *
 * While the switch is on, every /api request that would reach Render
 * (uncached analyses, metadata lookups, `/api/health`, the prewake) gets 503
 * `api_paused`, with `Retry-After` until the date (an hour without one) and
 * `details: {until, reason}`, before the governor or Render is called.
 * Request errors (404, 405, 413, 415) still come first, and everything that
 * never reaches Render keeps working: the static site and snapshot, the
 * metadata the Worker answers from it, KV-cached analyses,
 * `/api/_edge/health` (which reports the switch) and `/api/_ops/budget`.
 */

import { secondsUntil } from './budget'
import { RETRY_AFTER_API_PAUSED_UNKNOWN_S } from './constants'
import { errorResponse } from './http'

export const PAUSE_REASONS = ['render_quota', 'render_suspended', 'maintenance'] as const

export type PauseReason = (typeof PAUSE_REASONS)[number]

export interface ApiPause {
  /** ISO-8601 time the API is expected back, or null when unknown. */
  until: string | null
  reason: PauseReason | null
  /** The switch held text that is neither empty, a time nor `unknown`. */
  unreadable: boolean
  retryAfterSeconds: number
}

/** `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM[:SS]Z`, read as UTC. */
const UNTIL = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2}))?Z)?$/

/** Epoch ms of a switch time, or null when it is not one (including impossible dates). */
export function parseUntil(text: string): number | null {
  const match = UNTIL.exec(text)
  if (match === null) return null
  const [year, month, day, hour = '0', minute = '0', second = '0'] = match.slice(1)
  const parts = [year, month, day, hour, minute, second].map(Number) as [
    number,
    number,
    number,
    number,
    number,
    number,
  ]
  const at = Date.UTC(parts[0], parts[1] - 1, parts[2], parts[3], parts[4], parts[5])
  const date = new Date(at)
  const read = [
    date.getUTCFullYear(),
    date.getUTCMonth() + 1,
    date.getUTCDate(),
    date.getUTCHours(),
    date.getUTCMinutes(),
    date.getUTCSeconds(),
  ]
  return read.every((value, index) => value === parts[index]) ? at : null
}

/** The switch's state at `now`, or null when it is off. */
export function apiPause(
  env: Pick<Env, 'API_PAUSED_UNTIL' | 'API_PAUSED_REASON'>,
  now: number,
): ApiPause | null {
  const text = (env.API_PAUSED_UNTIL ?? '').trim()
  if (text === '') return null
  const reasonText = (env.API_PAUSED_REASON ?? '').trim()
  const reason = (PAUSE_REASONS as readonly string[]).includes(reasonText)
    ? (reasonText as PauseReason)
    : null
  if (text.toLowerCase() === 'unknown') {
    return {
      until: null,
      reason,
      unreadable: false,
      retryAfterSeconds: RETRY_AFTER_API_PAUSED_UNKNOWN_S,
    }
  }
  const at = parseUntil(text)
  if (at === null) {
    return {
      until: null,
      reason,
      unreadable: true,
      retryAfterSeconds: RETRY_AFTER_API_PAUSED_UNKNOWN_S,
    }
  }
  if (at <= now) return null
  return {
    until: new Date(at).toISOString(),
    reason,
    unreadable: false,
    retryAfterSeconds: secondsUntil(at, now),
  }
}

const WHY: Record<PauseReason, string> = {
  render_quota: "The analysis service's free hosting has used up this month's allowance",
  render_suspended: "The analysis service's free hosting has suspended it",
  maintenance: 'The analysis service is paused for maintenance',
}

/** 503 `api_paused`: the owner's switch is on. */
export function apiPausedResponse(pause: ApiPause, requestId: string): Response {
  const why = pause.reason === null ? 'The analysis service is paused' : WHY[pause.reason]
  const when =
    pause.until === null
      ? 'It is not known yet when it will be back.'
      : `It is expected back at ${pause.until.slice(0, 16).replace('T', ' ')} UTC.`
  return errorResponse(
    503,
    'api_paused',
    `${why}. ${when} The site's static pages, starter examples and saved results still work.`,
    requestId,
    {
      retryAfterSeconds: pause.retryAfterSeconds,
      details: { until: pause.until, reason: pause.reason },
    },
  )
}
