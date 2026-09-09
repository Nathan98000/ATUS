/**
 * Turns API/network failures into user-facing presentations.
 *
 * Analytical errors (422s) surface the API's own message — it is written for
 * exactly this purpose (e.g. the 2020 weighting explanation). Infrastructure
 * failures get friendly, actionable text. Technical detail (status, code,
 * request id) is preserved for a collapsible developer section, never as the
 * primary explanation.
 */
import { ApiError, NetworkError } from '../api/client'

export interface PresentedError {
  heading: string
  message: string
  hint: string | null
  /** Offer a "Try again" action (infrastructure problems only). */
  retryable: boolean
  /** Offer an "Adjust analysis" action (the request itself is the problem). */
  adjustable: boolean
  technical: string | null
}

export function presentError(error: unknown): PresentedError {
  if (error instanceof NetworkError) {
    return {
      heading: 'The analysis service could not be reached',
      message:
        'The request never got an answer. Check that the API is running and reachable, then try again.',
      hint: null,
      retryable: true,
      adjustable: false,
      technical: error.message,
    }
  }

  if (error instanceof ApiError) {
    const technical = `HTTP ${error.status} · ${error.code}${
      error.requestId ? ` · request ${error.requestId}` : ''
    }`
    switch (error.code) {
      case 'invalid_spec':
      case 'validation_error':
      case 'malformed_json':
        return {
          heading: 'This analysis request is not valid',
          message: error.message,
          hint: 'Adjust the analysis and run it again.',
          retryable: false,
          adjustable: true,
          technical,
        }
      case 'unknown_activity':
        return {
          heading: 'Unknown activity',
          message: error.message,
          hint: 'Pick an activity from the selector, which lists every code in the official lexicon.',
          retryable: false,
          adjustable: true,
          technical,
        }
      case 'unsupported_analysis':
        return {
          heading: 'This analysis is not statistically supported',
          message: error.message,
          hint: null,
          retryable: false,
          adjustable: true,
          technical,
        }
      case 'insufficient_data':
        return {
          heading: 'Not enough data for this analysis',
          message: error.message,
          hint: 'Try broadening the population filters or adding years.',
          retryable: false,
          adjustable: true,
          technical,
        }
      case 'database_unavailable':
      case 'schema_not_initialized':
      case 'data_not_loaded':
        return {
          heading: 'The analysis service is temporarily unavailable',
          message: 'The service is up but its database is not ready. Try again in a moment.',
          hint: null,
          retryable: true,
          adjustable: false,
          technical,
        }
      default:
        return {
          heading: 'The analysis service reported an unexpected problem',
          message:
            'The analysis could not be completed. This is a problem in the service, not in your request.',
          hint: 'Trying again may help; if it persists, the API logs have the details.',
          retryable: true,
          adjustable: false,
          technical,
        }
    }
  }

  return {
    heading: 'Something went wrong',
    message: 'An unexpected error occurred in the application.',
    hint: null,
    retryable: true,
    adjustable: false,
    technical: error instanceof Error ? `${error.name}: ${error.message}` : String(error),
  }
}
