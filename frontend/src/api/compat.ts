/**
 * API compatibility: this build speaks one major version of the HTTP
 * contract, `v1`. `/meta` reports the version the service speaks; anything
 * else — another major version, or a `/meta` whose shape this build cannot
 * read — is an `IncompatibleApiError`. That is never retried and never
 * papered over with defaults: the app shows one clear full-page state instead
 * of crashing a route or guessing (audit F25). A v1 `/meta` that reports no
 * loaded data is a `DataNotLoadedError` instead.
 */
import type { MetaResponse } from './types'

/** The API major version this build of the frontend supports. */
export const SUPPORTED_API_VERSION = 'v1'

export class IncompatibleApiError extends Error {
  /** The `api_version` the service reported (null when missing or unreadable). */
  readonly reportedVersion: string | null

  constructor(message: string, reportedVersion: string | null) {
    super(message)
    this.name = 'IncompatibleApiError'
    this.reportedVersion = reportedVersion
  }
}

/**
 * A compatible service with no survey data loaded: the API answers `/meta`
 * with release `unloaded` and no years before the first load. That is "no
 * data yet", not an API from another version (review round 2).
 */
export class DataNotLoadedError extends Error {
  constructor(message = 'The service reports no loaded survey data (no years).') {
    super(message)
    this.name = 'DataNotLoadedError'
  }
}

/** `v1`, `v1.2`, `v1-beta` → `v1`; anything unreadable → null. */
export function apiMajorVersion(version: unknown): string | null {
  if (typeof version !== 'string') return null
  const match = /^v(\d+)(?:[.-].*)?$/.exec(version.trim())
  return match ? `v${Number(match[1])}` : null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function isArrayOf(value: unknown, check: (item: unknown) => boolean): boolean {
  return Array.isArray(value) && value.every(check)
}

/**
 * Returns `/meta` typed once it is known to be a compatible, readable
 * response; throws `IncompatibleApiError` otherwise. Only the fields the UI
 * relies on are checked — unknown extra fields are ignored, as additive v1
 * changes must be.
 */
export function checkMetaCompatibility(value: unknown): MetaResponse {
  if (!isRecord(value)) {
    throw new IncompatibleApiError('The service metadata is not a JSON object.', null)
  }
  const reported = typeof value.api_version === 'string' ? value.api_version : null
  if (apiMajorVersion(value.api_version) !== SUPPORTED_API_VERSION) {
    throw new IncompatibleApiError(
      `The service speaks API ${reported ?? '(unknown)'}; this page needs ${SUPPORTED_API_VERSION}.`,
      reported,
    )
  }
  const data = value.data
  if (
    isRecord(data) &&
    (data.release === 'unloaded' || (Array.isArray(data.years) && data.years.length === 0))
  ) {
    throw new DataNotLoadedError()
  }
  const capabilities = value.capabilities
  const readable =
    typeof value.analytics_version === 'string' &&
    isRecord(data) &&
    typeof data.release === 'string' &&
    isArrayOf(data.years, (year) => Number.isInteger(year)) &&
    (data.years as unknown[]).length > 0 &&
    isRecord(capabilities) &&
    isArrayOf(
      capabilities.measures,
      (measure) => isRecord(measure) && typeof measure.name === 'string',
    ) &&
    isArrayOf(
      capabilities.weight_schemes,
      (scheme) => isRecord(scheme) && typeof scheme.name === 'string',
    ) &&
    isArrayOf(capabilities.variance_methods, (method) => typeof method === 'string')
  if (!readable) {
    throw new IncompatibleApiError(
      'The service metadata does not have the shape this page understands.',
      reported,
    )
  }
  return value as unknown as MetaResponse
}
