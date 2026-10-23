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
import {
  DataNotLoadedError,
  IncompatibleApiError,
  SUPPORTED_API_VERSION,
  apiMajorVersion,
} from '../api/compat'
import { COLD_START_BUDGET_MS } from '../api/resilience'

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
  /** Offer a "Change this chart" action (the request itself is the problem). */
  adjustable: boolean
  /** Seconds the service asked us to wait before trying again (drives a countdown). */
  retryAfterSeconds: number | null
  technical: string | null
}

const STILL_WORKS = 'Starter examples and analyses that were computed earlier still open.'

/**
 * Year queries at which the edge governor counts an analysis as heavy:
 * `len(years)`, doubled for a comparison (edge/src/constants.ts
 * `HEAVY_YEAR_QUERIES`; a test keeps the two equal).
 */
export const HEAVY_YEAR_QUERIES = 8

/** `data_not_loaded`, and a v1 `/meta` that reports no loaded data. */
const DATA_NOT_LOADED = {
  kind: 'service',
  heading: 'No survey data is loaded',
  message:
    'The analysis service is running, but its survey data is not loaded yet. Please try again later.',
  retryable: true,
} as const

/**
 * A "waking" failure (`resilience.ts`) is shown only after the automatic
 * retries used up the cold-start budget: the host has not answered for that
 * long, which a normal cold start does not take. Free hosting can also be
 * suspended for the rest of the month, and its answer then is undocumented
 * (docs/security.md, accepted risk R13), so the copy says that this may be
 * the cause instead of promising that the problem is temporary. Once the
 * owner sets the edge's pause switch, the answer is `api_paused` instead.
 */
const NOT_ANSWERING_FOR = `more than ${Math.floor(COLD_START_BUDGET_MS / 60_000)} minutes`
const MAYBE_PAUSED = `If it stays unavailable for hours, its free hosting may be paused, for example until next month after a monthly allowance ran out. ${STILL_WORKS}`

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
    // The right major version in a shape this page cannot read is not
    // "works with v1, but the service reported v1" (review round 2).
    const sameVersion = apiMajorVersion(error.reportedVersion) === SUPPORTED_API_VERSION
    return {
      ...base,
      kind: 'incompatible',
      heading: 'This page and the analysis service are out of step',
      message: sameVersion
        ? 'The analysis service’s metadata is not in the form this page reads. Reloading the page usually loads the matching version.'
        : `This version of ATUS Explorer works with analysis API ${SUPPORTED_API_VERSION}, but the service reported ${
            error.reportedVersion
              ? `version ${error.reportedVersion}`
              : 'something this page cannot read'
          }. Reloading the page usually loads the matching version.`,
      hint: 'If it persists, the service is being updated; please try again later.',
      technical: error.message,
    }
  }

  if (error instanceof DataNotLoadedError) {
    return { ...base, ...DATA_NOT_LOADED, technical: error.message }
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
    // The API's fallback for a domain rejection without a code of its own (422).
    case 'invalid_analysis':
      return {
        ...common,
        kind: 'request',
        heading: 'This analysis request is not valid',
        message: error.message,
        detailLines: validationDetailLines(error.details),
        hint: 'Change the chart and show it again.',
        adjustable: true,
      }
    case 'unknown_activity':
    case 'activity_not_found':
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
      // The edge's per-network daily allowance (details.budget 'prefix',
      // Retry-After up to the next 00:00 UTC) is not a per-minute limit.
      if (budgetOf(error) === 'prefix') {
        return {
          ...common,
          kind: 'limit',
          heading: 'Today’s new analyses from your network are used up',
          message: `To keep this free service available to everyone, each network can start only a limited number of new analyses per day, and yours has reached it. The limit resets at ${utcMidnightPhrase(now)}.`,
          hint: STILL_WORKS,
        }
      }
      if (retryAfter !== null && retryAfter > LONG_WAIT_SECONDS) {
        return {
          ...common,
          kind: 'limit',
          heading: 'New analyses are on hold for your network',
          message: `To keep this free service available to everyone, new analyses from your network are paused for a while. You can start new ones again ${resumeTimePhrase(retryAfter, now)}.`,
          hint: STILL_WORKS,
        }
      }
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
      // The edge governor's heavy-only cap: analyses below the heavy
      // threshold are still admitted (review round 2).
      if (budgetOf(error) === 'heavy') {
        return {
          ...common,
          kind: 'limit',
          heading: 'Today’s capacity for long analyses is used up',
          message: `This free service can run a limited number of long analyses per day — those covering ${HEAVY_YEAR_QUERIES} or more years (${HEAVY_YEAR_QUERIES / 2} or more for a comparison) — and today’s are used up. Analyses of up to ${HEAVY_YEAR_QUERIES - 1} years (up to ${HEAVY_YEAR_QUERIES / 2 - 1} for a comparison) still run. The limit resets at ${utcMidnightPhrase(now)}.`,
          hint: `Change the chart to fewer years. ${STILL_WORKS}`,
          adjustable: true,
        }
      }
      // Otherwise a shared allowance: the API sends it with details.scope
      // 'daily', the edge governor with 'global' or 'daily'. A per-network
      // daily denial is `rate_limited` with budget 'prefix' (above).
      return {
        ...common,
        kind: 'limit',
        heading: 'Today’s capacity for new analyses is used up',
        message: `This free service can run a limited number of new analyses per day, and today’s are used up. The limit resets at ${utcMidnightPhrase(now)}.`,
        hint: STILL_WORKS,
      }
    case 'capacity_paused':
      // The edge governor's monthly compute budget pauses whenever usage runs
      // ahead of its pacing line — early in the month too, often for an hour
      // or two — so it states neither "nearly used up" nor a monthly reset,
      // only when the pause lifts (review round 2).
      if (budgetOf(error) === 'cu_month') {
        return {
          ...common,
          kind: 'paused',
          heading: 'New analyses are paused for now',
          message: `ATUS Explorer runs entirely on free hosting. To spread this month’s free database allowance over the whole month, new analyses are paused for now and resume automatically${resumePhrase(resumesAtSeconds(error, now) ?? retryAfter, now)}.`,
          hint: STILL_WORKS,
        }
      }
      return {
        ...common,
        kind: 'paused',
        heading: 'New analyses are paused for now',
        message: `ATUS Explorer runs entirely on free hosting. This month’s database allowance is nearly used up, so new analyses are paused until it resets${resetPhrase(resumesAtSeconds(error, now) ?? retryAfter, now)}.`,
        hint: STILL_WORKS,
      }
    case 'api_paused':
      // The owner's pause switch at the edge (docs/api.md): set when the API's
      // free host is known to be suspended, which the edge cannot detect
      // itself. `details.until` is when it is expected back, or null.
      return {
        ...common,
        kind: 'paused',
        heading: 'The analysis service is paused',
        message: apiPausedMessage(error, now),
        hint: STILL_WORKS,
      }
    case 'database_paused':
      // Its Retry-After (a fixed hour) only paces retries: the provider's
      // allowance resets with its next billing period, which the API does not
      // know. A date is named only when the service sends `resumes_at`.
      return {
        ...common,
        kind: 'paused',
        heading: 'The analysis database is paused',
        message: `ATUS Explorer runs entirely on free hosting, and its database has used up its monthly allowance or was paused by the provider. It resumes automatically when the allowance resets${resetPhrase(resumesAtSeconds(error, now), now)}.`,
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
        heading: 'The analysis service is still not answering',
        message: `Free hosting sleeps when idle and usually wakes within a minute, but the analysis service has not answered for ${NOT_ANSWERING_FOR}. Try again in a few minutes.`,
        hint: MAYBE_PAUSED,
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
        retryAfterSeconds: retryAfter,
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
        ...upstreamUnavailable(error),
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
      return { ...common, ...DATA_NOT_LOADED, retryAfterSeconds: retryAfter }
    case 'forbidden_origin':
      // Through the site the edge Worker always adds its secret, so this
      // answer means the API does not accept the Worker's secret: the
      // deployment's configuration, never the visitor (review round 2).
      return {
        ...common,
        kind: 'app',
        heading: 'This request was refused',
        message:
          'The site’s connection to the analysis service is misconfigured, so new analyses cannot run. This is a problem in ATUS Explorer’s deployment, not in your analysis.',
        hint: STILL_WORKS,
      }
    // The API and the edge answer these when a request does not fit the HTTP
    // contract at all (an unknown path, method or content type). The page
    // only sends contract requests, so the likely cause is a page and a
    // service from different versions; repeating the request cannot help.
    case 'not_found':
    case 'method_not_allowed':
    case 'unsupported_media_type':
      return {
        ...common,
        kind: 'app',
        heading: 'The analysis service did not recognise this request',
        message:
          'This page sent a request the service does not handle. That usually means the page is out of date after an update of the service.',
        hint: 'Reload the page. If that does not help, the problem is in ATUS Explorer, not in your analysis.',
      }
    default:
      return {
        ...common,
        kind: 'service',
        heading: 'The analysis service reported an unexpected problem',
        message:
          'The analysis could not be completed. This is a problem in the service, not in your request.',
        hint: 'Trying again may help; if it keeps happening, please try again later.',
        retryable: true,
        retryAfterSeconds: retryAfter,
      }
  }
}

/**
 * `upstream_unavailable` by `details.reason` (the edge's, docs/api.md): only
 * a platform error page — or a non-JSON 5xx the client mapped itself, which
 * has no reason — means the hosting provider answered instead of the API.
 */
function upstreamUnavailable(
  error: ApiError,
): Pick<PresentedError, 'kind' | 'heading' | 'message' | 'hint'> {
  switch (detailString(error, 'reason')) {
    case 'connect_failed':
    case 'circuit_open':
      // Nothing answered; retried as a cold start until the budget ran out.
      return {
        kind: 'service',
        heading: 'The analysis service could not be reached',
        message: `The site could not connect to the analysis service for ${NOT_ANSWERING_FOR}. It may still be starting; try again in a few minutes.`,
        hint: MAYBE_PAUSED,
      }
    case 'not_configured':
      return {
        kind: 'service',
        heading: 'The analysis service could not be reached',
        message:
          'The site’s connection to the analysis service is not set up, so new analyses cannot run. This is a problem in ATUS Explorer’s deployment, not in your analysis.',
        hint: STILL_WORKS,
      }
    case 'origin_timeout':
      // The API took the request (a Cloudflare 524); retried once.
      return {
        kind: 'service',
        heading: 'The analysis service did not answer in time',
        message:
          'The service took the analysis but did not answer within the time its host allows. Trying again may return the finished result.',
        hint: null,
      }
    case 'broken_response':
      return {
        kind: 'service',
        heading: 'The analysis service’s answer broke off',
        message:
          'The connection broke off while the service was answering. Trying again usually works.',
        hint: null,
      }
    default:
      // `platform_error`, or a non-JSON 5xx page: retried until the budget ran out.
      return {
        kind: 'service',
        heading: 'The analysis service is unavailable',
        message: `Its hosting provider answered instead of the service for ${NOT_ANSWERING_FOR}. Try again in a few minutes.`,
        hint: MAYBE_PAUSED,
      }
  }
}

function detailString(error: ApiError, name: string): string | null {
  const value = (error.details as Record<string, unknown> | null)?.[name]
  return typeof value === 'string' ? value : null
}

/** `api_paused` by `details.reason`, with `details.until` as a date when it is known. */
function apiPausedMessage(error: ApiError, now: number): string {
  const until = detailString(error, 'until')
  const at = until === null ? Number.NaN : Date.parse(until)
  const date =
    Number.isNaN(at) || at <= now
      ? null
      : new Date(at).toLocaleDateString(undefined, { month: 'long', day: 'numeric' })
  const back =
    date === null
      ? 'It is not known yet when it will be back.'
      : `It is expected back around ${date}.`
  switch (detailString(error, 'reason')) {
    case 'render_quota':
      return `ATUS Explorer runs entirely on free hosting. The analysis service’s host has used up this month’s allowance, so new analyses are paused until it resets${date === null ? '' : ` (around ${date})`}.`
    case 'render_suspended':
      return `ATUS Explorer runs entirely on free hosting, and the analysis service’s host has suspended it, so new analyses are paused. ${back}`
    case 'maintenance':
      return `The analysis service is paused for maintenance, so new analyses cannot run for now. ${back}`
    default:
      return `The analysis service is paused, so new analyses cannot run for now. ${back}`
  }
}

/** Seconds until `details.resumes_at` (an ISO timestamp), when the service sent one. */
function resumesAtSeconds(error: ApiError, now: number): number | null {
  const resumesAt = detailString(error, 'resumes_at')
  const at = resumesAt === null ? Number.NaN : Date.parse(resumesAt)
  return Number.isNaN(at) ? null : Math.ceil((at - now) / 1000)
}

/** A rate limit asking for more than this is not "a few per minute": no countdown. */
const LONG_WAIT_SECONDS = 15 * 60

/** The edge governor's budget name (`details.budget`), when it sent one. */
export function budgetOf(error: ApiError): string | null {
  return detailString(error, 'budget')
}

/** "at about 3:45 PM" (local), or "tomorrow at about 3:45 PM". */
function resumeTimePhrase(seconds: number, now: number): string {
  const at = new Date(now + seconds * 1000)
  const time = at.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
  const sameDay = at.toDateString() === new Date(now).toDateString()
  return `${sameDay ? '' : 'tomorrow '}at about ${time}`
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

/**
 * " at about 3:45 PM" (local) within the day, " (around October 31)" later,
 * '' when the service did not say.
 */
function resumePhrase(seconds: number | null, now: number): string {
  if (seconds === null || seconds <= 0) return ''
  if (seconds < 20 * 60 * 60) return ` ${resumeTimePhrase(seconds, now)}`
  return resetPhrase(seconds, now)
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
 * Validation details → short human-readable lines locating each problem. The
 * API's envelope lists `{errors: [{location, message, type}]}` (docs/api.md);
 * FastAPI's own `{loc, msg}` is read too. Unknown shapes yield nothing.
 */
function validationDetailLines(details: unknown): string[] {
  const errors = (details as { errors?: unknown })?.errors
  if (!Array.isArray(errors)) return []
  return errors.slice(0, 6).flatMap((entry) => {
    const item = entry as {
      location?: unknown
      message?: unknown
      loc?: unknown
      msg?: unknown
    }
    const message = item.message ?? item.msg
    if (typeof message !== 'string') return []
    const parts = item.location ?? item.loc
    const location = Array.isArray(parts)
      ? parts.filter((part) => part !== 'body').join('.')
      : ''
    return [location !== '' ? `${location}: ${message}` : message]
  })
}
