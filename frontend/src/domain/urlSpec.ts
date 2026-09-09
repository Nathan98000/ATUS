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
    parsed = JSON.parse(json)
  } catch {
    throw new InvalidShareLinkError('The analysis link does not contain a valid specification.')
  }

  return validateRequestShape(parsed, operation)
}

/**
 * Narrow structural validation — enough to reject nonsense links before
 * making a request. The API remains the authoritative validator; anything
 * structurally plausible is sent and the API's structured error is shown.
 */
export function validateRequestShape(value: unknown, operation: Operation): AnalysisRequest {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new InvalidShareLinkError('The analysis specification must be an object.')
  }
  const spec = value as Record<string, unknown>

  const activity = spec.activity
  if (activity === null || typeof activity !== 'object' || Array.isArray(activity)) {
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

  if (operation === 'compare') {
    for (const group of ['group_a', 'group_b'] as const) {
      const g = spec[group]
      if (g === null || typeof g !== 'object' || Array.isArray(g)) {
        throw new InvalidShareLinkError('A comparison link must define both groups.')
      }
    }
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
