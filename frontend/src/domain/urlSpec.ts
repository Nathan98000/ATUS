/**
 * Shareable analysis URLs.
 *
 * An analysis is fully described by (operation, request spec). The share URL
 * encodes exactly that — `/analysis/<operation>?spec=<base64url(JSON)>` — so
 * any browser can reconstruct and re-run the analysis with no server-side
 * state (the Phase 3 API is stateless by design).
 *
 * Canonicalization: after a successful run, the page rewrites the URL using
 * the API's own canonical `spec` from the response (estimate/trend) so that
 * equivalent requests (differently ordered years or code lists) share one
 * canonical link, exactly as the backend treats them as one analysis. The
 * JSON itself is serialized with sorted keys for the same reason. Compare
 * responses carry canonical per-group specs but no single top-level spec, so
 * compare links keep the sorted-keys request encoding.
 */
import type { AnalysisRequest, CompareRequest, Operation } from '../api/types'
import { stableStringify } from '../utils/stableStringify'

export class InvalidShareLinkError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'InvalidShareLinkError'
  }
}

/** UTF-8 → base64url (RFC 4648 §5, no padding). */
export function encodeSpec(spec: AnalysisRequest): string {
  const bytes = new TextEncoder().encode(stableStringify(spec))
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '')
}

export function decodeSpec(encoded: string, operation: Operation): AnalysisRequest {
  let json: string
  try {
    const base64 = encoded.replaceAll('-', '+').replaceAll('_', '/')
    const binary = atob(base64)
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0))
    json = new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  } catch {
    throw new InvalidShareLinkError('The analysis link could not be decoded.')
  }

  let parsed: unknown
  try {
    // The reviver rejects prototype-pollution key names outright: they can
    // never be legitimate spec fields, and letting them through would make
    // the POSTed body diverge from the sorted-keys cache key / share URL.
    parsed = JSON.parse(json, (key, value) => {
      if (key === '__proto__' || key === 'constructor' || key === 'prototype') {
        throw new InvalidShareLinkError('The analysis link contains an invalid field name.')
      }
      return value as unknown
    })
  } catch (error) {
    if (error instanceof InvalidShareLinkError) throw error
    throw new InvalidShareLinkError('The analysis link does not contain a valid specification.')
  }

  if (exceedsDepth(parsed, MAX_SPEC_DEPTH)) {
    throw new InvalidShareLinkError(
      'The analysis link does not contain a valid specification (it is nested too deeply).',
    )
  }
  return validateRequestShape(parsed, operation)
}

/**
 * Objects and arrays in a specification nest at most 3 deep (spec → activity
 * → include). A link far deeper is not a specification, and one deep enough
 * (thousands of levels fit in a URL) overflowed the stack in the recursive
 * canonical serialization (`stableStringify`), so the app's error boundary
 * replaced the page instead of this module's invalid-link message.
 */
const MAX_SPEC_DEPTH = 10

/** Whether objects/arrays in `value` nest deeper than `limit` (iterative: no recursion). */
function exceedsDepth(value: unknown, limit: number): boolean {
  const pending: [unknown, number][] = [[value, 1]]
  for (let next = pending.pop(); next !== undefined; next = pending.pop()) {
    const [item, depth] = next
    if (item === null || typeof item !== 'object') continue
    if (depth > limit) return true
    for (const child of Object.values(item)) pending.push([child, depth + 1])
  }
  return false
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

const isString = (value: unknown) => typeof value === 'string'
/**
 * A finite number. A link's `1e400` parses as Infinity, which the page
 * described ("age Infinity+") while the POSTed JSON sent `null`, so the API
 * applied no filter at all (review round 4).
 */
const isNumber = (value: unknown) => typeof value === 'number' && Number.isFinite(value)
const isBoolean = (value: unknown) => typeof value === 'boolean'
/**
 * A date as the API writes it and the builder's date input produces it:
 * YYYY-MM-DD (the input allows years past 9999, which the API refuses with
 * its own message). The API also reads a string of digits as a Unix time
 * ("1577836800" is 2020-01-01), which the page would have shown as
 * "from 1577836800" (review round 4).
 */
const isIsoDate = (value: unknown) =>
  typeof value === 'string' && /^\d{4,}-\d{2}-\d{2}$/.test(value)
const isScalar = (value: unknown) => isString(value) || isNumber(value) || isBoolean(value)
const isStringList = (value: unknown) => Array.isArray(value) && value.every(isString)

/**
 * The population dimensions the page describes, with the JSON type it reads
 * each as. The API reads its fields leniently — "no", "false", "0" and "off"
 * are all `has_household_children: false` to it — while the page described
 * any non-empty string as "with household children", so a hand-written link
 * was computed for one population and labelled with the opposite one
 * (review round 4). Other dimensions (unknown to the page) may hold any
 * scalar; the API refuses them.
 */
const POPULATION_TYPES = new Map<string, (value: unknown) => boolean>([
  ['age_min', isNumber],
  ['age_max', isNumber],
  ['sex', isString],
  ['employment_status', isString],
  ['has_household_children', isBoolean],
  ['region', isNumber],
  ['state_fips', isString],
  ['education_level', isString],
  ['day_type', isString],
  ['diary_date_min', isIsoDate],
  ['diary_date_max', isIsoDate],
])

/** A population filter: an object of scalar values (null when unset), each of its dimension's type. */
const isPopulation = (value: unknown) =>
  isPlainObject(value) &&
  Object.entries(value).every(
    ([dimension, item]) => item === null || (POPULATION_TYPES.get(dimension) ?? isScalar)(item),
  )

/**
 * Fields the page reads, with the type it reads them as. A field may be
 * absent or null (unset); any other type is refused here, because the page
 * renders these values and calls string methods on them: a numeric preset
 * crashed the result page into the error boundary, an object label crashed
 * the builder, and a numeric group label made Analyze throw (review round 2).
 */
const FIELD_TYPES: readonly [path: string, check: (value: unknown) => boolean][] = [
  ['activity.preset', isString],
  ['activity.include', isStringList],
  ['activity.exclude', isStringList],
  ['activity.label', isString],
  ['measure', isString],
  ['weights', isString],
  ['variance', isString],
  ['confidence_level', isNumber],
  ['population', isPopulation],
]
const COMPARE_FIELD_TYPES: typeof FIELD_TYPES = [
  ['label_a', isString],
  ['label_b', isString],
]

function fieldAt(spec: Record<string, unknown>, path: string): unknown {
  const [first, second] = path.split('.') as [string, string | undefined]
  const value = spec[first]
  return second === undefined ? value : (value as Record<string, unknown>)[second]
}

function checkFieldTypes(spec: Record<string, unknown>, fields: typeof FIELD_TYPES): void {
  for (const [path, check] of fields) {
    const value = fieldAt(spec, path)
    if (value !== undefined && value !== null && !check(value)) {
      throw new InvalidShareLinkError(
        `The analysis specification is not valid: its ${path} has the wrong type.`,
      )
    }
  }
}

/**
 * Narrow structural validation — enough to reject nonsense links before
 * making a request or rendering them. The API remains the authoritative
 * validator; anything structurally plausible is sent and the API's
 * structured error is shown.
 */
export function validateRequestShape(value: unknown, operation: Operation): AnalysisRequest {
  if (!isPlainObject(value)) {
    throw new InvalidShareLinkError('The analysis specification must be an object.')
  }
  const spec = value

  const activity = spec.activity
  if (!isPlainObject(activity)) {
    throw new InvalidShareLinkError('The analysis specification is missing its activity.')
  }

  const years = spec.years
  if (
    !Array.isArray(years) ||
    years.length === 0 ||
    !years.every((y) => typeof y === 'number')
  ) {
    throw new InvalidShareLinkError('The analysis specification is missing its years.')
  }

  checkFieldTypes(spec, FIELD_TYPES)

  if (operation === 'compare') {
    for (const group of ['group_a', 'group_b'] as const) {
      if (!isPlainObject(spec[group])) {
        throw new InvalidShareLinkError('A comparison link must define both groups.')
      }
    }
    checkFieldTypes(spec, [
      ...COMPARE_FIELD_TYPES,
      ['group_a', isPopulation],
      ['group_b', isPopulation],
    ])
    return spec as unknown as CompareRequest
  }

  return spec as unknown as AnalysisRequest
}

export function analysisPath(operation: Operation, spec: AnalysisRequest): string {
  return `/analysis/${operation}?spec=${encodeSpec(spec)}`
}

export function explorePath(operation: Operation, spec: AnalysisRequest): string {
  return `/explore?op=${operation}&spec=${encodeSpec(spec)}`
}
