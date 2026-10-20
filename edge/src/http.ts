/**
 * Response helpers shared by every /api response the Worker returns.
 *
 * `_headers` does not apply to Worker-generated responses, so the security
 * headers are set here in code, on proxied and Worker-generated responses
 * alike. Errors use the API's one envelope,
 *
 *     {"error": {"code": "...", "message": "...", "details": {...} | null}}
 *
 * serialized compactly, the way the API serializes it.
 */

/** Set on every /api response; `Cache-Control: no-store` keeps errors and results out of every cache. */
export const SECURITY_HEADERS: Readonly<Record<string, string>> = {
  'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'",
  'Strict-Transport-Security': 'max-age=31536000',
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'Cross-Origin-Resource-Policy': 'same-origin',
  'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), payment=(), usb=()',
  'Cache-Control': 'no-store',
}

/** Overwrite (never merge) the security headers onto `headers`. */
export function applySecurityHeaders(headers: Headers): Headers {
  for (const [name, value] of Object.entries(SECURITY_HEADERS)) headers.set(name, value)
  return headers
}

/**
 * A request ID the API accepts from an authenticated edge
 * (`^[A-Za-z0-9._-]{8,64}$`): 32 hex characters from a random UUID.
 */
export function newRequestId(): string {
  return crypto.randomUUID().replaceAll('-', '')
}

/** A JSON response with the security headers and the request ID. */
export function jsonResponse(
  status: number,
  payload: unknown,
  requestId: string,
  extraHeaders: Record<string, string> = {},
): Response {
  const headers = new Headers({ 'Content-Type': 'application/json', ...extraHeaders })
  headers.set('X-Request-ID', requestId)
  applySecurityHeaders(headers)
  return new Response(JSON.stringify(payload), { status, headers })
}

export interface ErrorOptions {
  details?: Record<string, unknown> | null
  /** Whole seconds; also copied into `details.retry_after_seconds`. */
  retryAfterSeconds?: number
  headers?: Record<string, string>
}

/** A Worker-generated error in the API's envelope. */
export function errorResponse(
  status: number,
  code: string,
  message: string,
  requestId: string,
  options: ErrorOptions = {},
): Response {
  const headers: Record<string, string> = { ...options.headers }
  let details = options.details ?? null
  if (options.retryAfterSeconds !== undefined) {
    const seconds = Math.max(1, Math.ceil(options.retryAfterSeconds))
    headers['Retry-After'] = String(seconds)
    details = { ...details, retry_after_seconds: seconds }
  }
  return jsonResponse(status, { error: { code, message, details } }, requestId, headers)
}

/** True when a Content-Type header names JSON (`application/json`, optionally with parameters). */
export function isJsonContentType(value: string | null): boolean {
  return mediaType(value) === 'application/json'
}

/** True when a Content-Type header names HTML. */
export function isHtmlContentType(value: string | null): boolean {
  return mediaType(value) === 'text/html'
}

function mediaType(value: string | null): string {
  return (value ?? '').split(';', 1)[0]!.trim().toLowerCase()
}

/**
 * Read a body stream up to `limit` bytes. Returns the bytes, or `null` as
 * soon as the stream exceeds the limit (the rest is cancelled, not read).
 */
export async function readCapped(
  body: ReadableStream<Uint8Array> | null,
  limit: number,
): Promise<Uint8Array | null> {
  if (body === null) return new Uint8Array(0)
  const reader = body.getReader()
  const chunks: Uint8Array[] = []
  let total = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    total += value.byteLength
    if (total > limit) {
      await reader.cancel()
      return null
    }
    chunks.push(value)
  }
  const bytes = new Uint8Array(total)
  let offset = 0
  for (const chunk of chunks) {
    bytes.set(chunk, offset)
    offset += chunk.byteLength
  }
  return bytes
}
