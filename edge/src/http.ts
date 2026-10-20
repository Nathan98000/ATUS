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
  const read = await readUpTo(body, limit)
  if (read.complete) return read.bytes
  await read.stream.cancel()
  return null
}

export type ReadUpTo =
  | { complete: true; bytes: Uint8Array }
  | { complete: false; stream: ReadableStream<Uint8Array> }

/**
 * Read a body stream to its end if it is at most `limit` bytes. A longer
 * body is not lost: the result is a stream of the bytes already read
 * followed by the rest as it arrives. Rejects if the stream fails while the
 * first `limit + 1` bytes are being read.
 */
export async function readUpTo(
  body: ReadableStream<Uint8Array> | null,
  limit: number,
): Promise<ReadUpTo> {
  if (body === null) return { complete: true, bytes: new Uint8Array(0) }
  const reader = body.getReader()
  const chunks: Uint8Array[] = []
  let total = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) return { complete: true, bytes: concatBytes(chunks, total) }
    chunks.push(value)
    total += value.byteLength
    if (total > limit) {
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          for (const chunk of chunks) controller.enqueue(chunk)
        },
        async pull(controller) {
          const next = await reader.read()
          if (next.done) controller.close()
          else controller.enqueue(next.value)
        },
        cancel(reason) {
          return reader.cancel(reason)
        },
      })
      return { complete: false, stream }
    }
  }
}

/** Concatenate `chunks` (`total` bytes in all). */
export function concatBytes(chunks: readonly Uint8Array[], total?: number): Uint8Array {
  const bytes = new Uint8Array(
    total ?? chunks.reduce((sum, chunk) => sum + chunk.byteLength, 0),
  )
  let offset = 0
  for (const chunk of chunks) {
    bytes.set(chunk, offset)
    offset += chunk.byteLength
  }
  return bytes
}

/** Lowercase hex of `bytes`. */
export function toHex(bytes: ArrayBuffer | Uint8Array): string {
  return Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2, '0')).join(
    '',
  )
}

/** SHA-256 of `data` (UTF-8 for strings) as lowercase hex. */
export async function sha256Hex(data: string | Uint8Array): Promise<string> {
  const bytes = typeof data === 'string' ? new TextEncoder().encode(data) : data
  return toHex(await crypto.subtle.digest('SHA-256', bytes))
}

/** The 404 for anything outside the allowlist (and for ops routes without the bearer token). */
export function notFoundResponse(requestId: string): Response {
  return errorResponse(404, 'not_found', 'The requested API route does not exist.', requestId)
}
