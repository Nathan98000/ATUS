/**
 * Turns API/network failures into user-facing presentations.
 *
 * Analytical errors (422s) surface the API's own message — it is written for
 * exactly this purpose (e.g. the 2020 weighting explanation). Operational
 * states get plain, honest text written for the public: what happened, that
 * it is not the visitor's fault, what still works, and when it will clear.
 * Every error code of the v1 contract has a presentation, including the ones
 * the edge Worker generates and `edge_daily_limit`, which the client
 * synthesizes for a non-JSON 429. Technical detail (status, code, request id)
 * is kept for a collapsible developer section, never the primary explanation.
 */
import { ApiError, NetworkError } from '../api/client'
import { IncompatibleApiError, SUPPORTED_API_VERSION } from '../api/compat'

/**
 * - `request`: the analysis request itself is the problem (adjust it);
 * - `service`: a temporary service problem (try again);
 * - `limit`: a rate or daily limit for new analyses (wait, then try again);
 * - `paused`: the free hosting quota is exhausted until it resets — shown as
 *   a full-page state instead of a cascade of errors;
 * - `incompatible`: this page cannot talk to the service's API version — a
 *   full-page state;
 * - `app`: an unexpected problem in the application.
 */
export type ErrorKind = 'request' | 'service' | 'limit' | 'paused' | 'incompatible' | 'app'

export interface PresentedError {
  kind: ErrorKind
  heading: string
  message: string
  /** Field-level problems from the API's validation details, if any. */
  detailLines: string[]
  hint: string | null
  /** Offer a "Try again" action. */
  retryable: boolean
  /** Offer an "Adjust analysis" action (the request itself is the problem). */
  adjustable: boolean
  /** Seconds the service asked us to wait before trying again (drives a countdown). */
  retryAfterSeconds: number | null
  technical: string | null
}

const STILL_WORKS = 'Starter examples and analyses that were computed earlier still open.'

/** True for states presented as a full page (paused quota, incompatible API). */
export function isFullPageError(error: unknown): boolean {
  const kind = presentError(error).kind
  return kind === 'paused' || kind === 'incompatible'
}

export function presentError(error: unknown, now: number = Date.now()): PresentedError {
  const base = {
    detailLines: [] as string[],
    hint: null,
    retryable: false,
    adjustable: false,
    retryAfterSeconds: null,
  }

  if (error instanceof IncompatibleApiError) {
    return {
      ...base,
      kind: 'incompatible',
      heading: 'This page and the analysis service are out of step',
      message: `This version of ATUS Explorer works with analysis API ${SUPPORTED_API_VERSION}, but the service reported ${
        error.reportedVersion
          ? `version ${error.reportedVersion}`
          : 'something this page cannot read'
      }. Reloading the page usually loads the matching version.`,
      hint: 'If it persists, the service is being updated; please try again later.',
      technical: error.message,
    }
  }

  if (error instanceof NetworkError) {
    return {
      ...base,
      kind: 'service',
      heading: 'The analysis service could not be reached',
      message: error.timedOut
        ? 'The service did not answer in time. It may be waking up or busy; try again in a moment.'
        : 'The request never got an answer. Check your internet connection and try again; the service may also be restarting.',
      retryable: true,
      technical: error.message,
    }
  }

  if (error instanceof ApiError) {
    return presentApiError(error, base, now)
  }

  return {
    ...base,
    kind: 'app',
    heading: 'Something went wrong',
    message: 'An unexpected error occurred in the application.',
    retryable: true,
    technical: error instanceof Error ? `${error.name}: ${error.message}` : String(error),
  }
}

type Base = Pick<
  PresentedError,
  'detailLines' | 'hint' | 'retryable' | 'adjustable' | 'retryAfterSeconds'
>

function presentApiError(error: ApiError, base: Base, now: number): PresentedError {
  const technical = `HTTP ${error.status} · ${error.code}${
    error.requestId ? ` · request ${error.requestId}` : ''
  }`
  const common = { ...base, technical }
  const retryAfter = error.retryAfterSeconds

  switch (error.code) {
    case 'invalid_spec':
    case 'validation_error':
    case 'malformed_json':
      return {
        ...common,
        kind: 'request',
        heading: 'This analysis request is not valid',
        message: error.message,
        detailLines: validationDetailLines(error.details),
        hint: 'Adjust the analysis and run it again.',
        adjustable: true,
      }
    case 'unknown_activity':
      return {
        ...common,
        kind: 'request',
        heading: 'Unknown activity',
        message: error.message,
        hint: 'Pick an activity from the selector, which lists every code in the official lexicon.',
        adjustable: true,
      }
    case 'unsupported_analysis':
      return {
        ...common,
        kind: 'request',
        heading: 'This analysis is not statistically supported',
        message: error.message,
        adjustable: true,
      }
    case 'insufficient_data':
      return {
        ...common,
        kind: 'request',
        heading: 'Not enough data for this analysis',
        message: error.message,
        hint: 'Try broadening the population filters or adding years.',
        adjustable: true,
      }
    case 'payload_too_large':
      return {
        ...common,
        kind: 'request',
        heading: 'This analysis request is too large',
        message: 'It is larger than the analysis service accepts.',
        hint: 'Use fewer activity codes or years.',
        adjustable: true,
      }

    case 'rate_limited':
      return {
        ...common,
        kind: 'limit',
        heading: 'Too many new analyses in a short time',
        message: `To keep this free service available to everyone, each visitor can start only a few new analyses per minute. ${waitPhrase(retryAfter)}`,
        hint: STILL_WORKS,
        retryable: true,
        retryAfterSeconds: retryAfter,
      }
    case 'daily_capacity_reached':
      return {
        ...common,
        kind: 'limit',
        heading: 'Today’s capacity for new analyses is used up',
        message: `${
          scopeOf(error) === 'client'
            ? 'Your network has started as many new analyses today as this free service allows for one visitor.'
            : 'This free service can run a limited number of new analyses per day, and today’s are used up.'
        } The limit resets at ${utcMidnightPhrase(now)}.`,
        hint: STILL_WORKS,
      }
    case 'capacity_paused':
      return {
        ...common,
        kind: 'paused',
        heading: 'New analyses are paused for now',
        message: `ATUS Explorer runs entirely on free hosting. This month’s database allowance is nearly used up, so new analyses are paused until it resets${resetPhrase(retryAfter, now)}.`,
        hint: STILL_WORKS,
      }
    case 'database_paused':
      return {
        ...common,
        kind: 'paused',
        heading: 'The analysis database is paused',
        message: `ATUS Explorer runs entirely on free hosting, and its database has used up its monthly allowance or was paused by the provider. It resumes automatically when the allowance resets${resetPhrase(retryAfter, now)}.`,
        hint: STILL_WORKS,
      }
    case 'edge_daily_limit':
      return {
        ...common,
        kind: 'paused',
        heading: 'ATUS Explorer has reached today’s request limit',
        message: `The free hosting plan allows a fixed number of requests per day across all visitors, and today’s are used up. Service resumes at ${utcMidnightPhrase(now)}.`,
        hint: 'The home page and its starter examples may still work.',
      }

    case 'service_waking':
      return {
        ...common,
        kind: 'service',
        heading: 'The analysis service is still waking up',
        message:
          'Free hosting sleeps when idle, and waking it is taking longer than usual. Try again in a moment.',
        retryable: true,
        retryAfterSeconds: retryAfter,
      }
    case 'overloaded':
      return {
        ...common,
        kind: 'service',
        heading: 'The analysis service is busy',
        message:
          'It is already running as many analyses as this free deployment can handle at once. Try again in a few seconds.',
        retryable: true,
        retryAfterSeconds: retryAfter,
      }
    case 'analysis_timeout':
      return {
        ...common,
        kind: 'service',
        heading: 'This analysis took too long',
        message:
          'It ran past the service’s time limit and was stopped. Long periods with standard errors are the slowest analyses; fewer years usually help.',
        hint: 'Trying again later, when the service is less busy, may also work.',
        retryable: true,
        adjustable: true,
      }
    case 'capacity_unavailable':
      return {
        ...common,
        kind: 'service',
        heading: 'New analyses are temporarily unavailable',
        message:
          'The part of the service that budgets this free deployment’s capacity could not be reached, so new analyses are held back to protect the free allowance.',
        hint: `${STILL_WORKS} Try again in a few minutes.`,
        retryable: true,
        retryAfterSeconds: retryAfter,
      }
    case 'data_version_mismatch':
      return {
        ...common,
        kind: 'service',
        heading: 'The service is switching to a new data release',
        message:
          'For a few minutes during an update, new analyses are held back so that every result comes from a single release. Try again shortly.',
        retryable: true,
        retryAfterSeconds: retryAfter,
      }
    case 'upstream_unavailable':
      return {
        ...common,
        kind: 'service',
        heading: 'The analysis service is unavailable',
        message:
          'Its hosting provider answered instead of the service. This is usually temporary; try again in a minute.',
        retryable: true,
        retryAfterSeconds: retryAfter,
      }
    case 'database_unavailable':
    case 'schema_not_initialized':
      return {
        ...common,
        kind: 'service',
        heading: 'The analysis database is temporarily unavailable',
        message:
          'The service could not use its database just now. This is usually temporary; try again in a moment.',
        retryable: true,
        retryAfterSeconds: retryAfter,
      }
    case 'data_not_loaded':
      return {
        ...common,
        kind: 'service',
        heading: 'No survey data is loaded',
        message:
          'The analysis service is running, but its survey data is not loaded yet. Please try again later.',
        retryable: true,
      }
    case 'forbidden_origin':
      return {
        ...common,
        kind: 'app',
        heading: 'This request was refused',
        message:
          'The analysis service only accepts requests sent through ATUS Explorer’s own site.',
      }
    default:
      return {
        ...common,
        kind: 'service',
        heading: 'The analysis service reported an unexpected problem',
        message:
          'The analysis could not be completed. This is a problem in the service, not in your request.',
        hint: 'Trying again may help; if it persists, the API logs have the details.',
        retryable: true,
      }
  }
}

function scopeOf(error: ApiError): string | null {
  const scope = (error.details as { scope?: unknown } | null)?.scope
  return typeof scope === 'string' ? scope : null
}

function waitPhrase(seconds: number | null): string {
  if (seconds === null) return 'Please wait a minute before trying again.'
  if (seconds <= 1) return 'You can try again now.'
  if (seconds < 90) return `Please wait about ${seconds} seconds before trying again.`
  return `Please wait about ${Math.round(seconds / 60)} minutes before trying again.`
}

/** "00:00 UTC (8:00 PM EDT your time)" for the next UTC midnight. */
function utcMidnightPhrase(now: number): string {
  const today = new Date(now)
  const nextMidnight = Date.UTC(
    today.getUTCFullYear(),
    today.getUTCMonth(),
    today.getUTCDate() + 1,
  )
  const local = new Date(nextMidnight).toLocaleTimeString(undefined, {
    hour: 'numeric',
    minute: '2-digit',
    timeZoneName: 'short',
  })
  return `00:00 UTC (${local} your time)`
}

/** " (around October 1)" when the service said how long; '' otherwise. */
function resetPhrase(retryAfterSeconds: number | null, now: number): string {
  if (retryAfterSeconds === null || retryAfterSeconds <= 0) return ''
  const date = new Date(now + retryAfterSeconds * 1000).toLocaleDateString(undefined, {
    month: 'long',
    day: 'numeric',
  })
  return ` (around ${date})`
}

/**
 * FastAPI validation details ({errors: [{loc, msg, ...}]}) → short
 * human-readable lines locating each problem. Unknown shapes yield nothing.
 */
function validationDetailLines(details: unknown): string[] {
  const errors = (details as { errors?: unknown })?.errors
  if (!Array.isArray(errors)) return []
  return errors.slice(0, 6).flatMap((entry) => {
    const item = entry as { loc?: unknown; msg?: unknown }
    if (typeof item.msg !== 'string') return []
    const location = Array.isArray(item.loc)
      ? item.loc.filter((part) => part !== 'body').join('.')
      : ''
    return [location !== '' ? `${location}: ${item.msg}` : item.msg]
  })
}
