/**
 * The single HTTP boundary between the frontend and the Phase 3 API.
 *
 * Every request goes through `apiRequest`: one place for the base URL, the
 * /api/v1 prefix, JSON handling, and the translation of failures into two
 * distinct error types the UI can present differently:
 *
 * - `ApiError` — the backend answered with its structured error envelope
 *   (an analytical or validation problem, or a 5xx it chose to report).
 * - `NetworkError` — the backend could not be reached, timed out, or sent
 *   something that is not the API's JSON (an infrastructure problem).
 */

export const API_VERSION_PREFIX = '/api/v1'

export function apiBaseUrl(): string {
  const configured = import.meta.env.VITE_API_BASE_URL as string | undefined
  const base = configured && configured.trim() !== '' ? configured : 'http://localhost:8000'
  return base.replace(/\/+$/, '')
}

/** A structured error returned by the API's one error envelope. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: unknown
  readonly requestId: string | null

  constructor(
    status: number,
    code: string,
    message: string,
    details: unknown,
    requestId: string | null,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
    this.requestId = requestId
  }
}

/** The API could not be reached or did not speak the expected protocol. */
export class NetworkError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options)
    this.name = 'NetworkError'
  }
}

export interface ApiResult<T> {
  data: T
  /** X-Analysis-Key: deterministic identifier of an analysis (analysis endpoints only). */
  analysisKey: string | null
  /** X-Cache: hit | miss | bypass (analysis endpoints only). */
  cache: string | null
}

interface RequestOptions {
  method?: 'GET' | 'POST'
  body?: unknown
  signal?: AbortSignal
  /** Prefix the path with /api/v1 (default true; false only for /health, /ready). */
  versioned?: boolean
}

async function parseErrorEnvelope(response: Response): Promise<ApiError> {
  const requestId = response.headers.get('X-Request-ID')
  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    return new ApiError(
      response.status,
      'malformed_response',
      `The API returned an unreadable ${response.status} response.`,
      null,
      requestId,
    )
  }
  const envelope = payload as { error?: { code?: string; message?: string; details?: unknown } }
  if (envelope && envelope.error && typeof envelope.error.message === 'string') {
    return new ApiError(
      response.status,
      envelope.error.code ?? 'unknown_error',
      envelope.error.message,
      envelope.error.details ?? null,
      requestId,
    )
  }
  return new ApiError(
    response.status,
    'malformed_response',
    `The API returned an unexpected ${response.status} response.`,
    null,
    requestId,
  )
}

export async function apiRequest<T>(
  path: string,
  options: RequestOptions = {},
): Promise<ApiResult<T>> {
  const { method = 'GET', body, signal, versioned = true } = options
  const url = apiBaseUrl() + (versioned ? API_VERSION_PREFIX : '') + path

  let response: Response
  try {
    response = await fetch(url, {
      method,
      signal,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause
    throw new NetworkError('The analysis service could not be reached.', { cause })
  }

  if (!response.ok) {
    throw await parseErrorEnvelope(response)
  }

  let data: T
  try {
    data = (await response.json()) as T
  } catch (cause) {
    throw new NetworkError('The API returned a response that could not be read as JSON.', {
      cause,
    })
  }

  return {
    data,
    analysisKey: response.headers.get('X-Analysis-Key'),
    cache: response.headers.get('X-Cache'),
  }
}
