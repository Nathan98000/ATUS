/**
 * Origin responses (ADR-011 step 7): API JSON and error envelopes pass
 * through byte for byte; platform responses that are not the API's become
 * `service_waking` (503), `overloaded` (503, uvicorn's own connection-limit
 * answer) or `upstream_unavailable` (502, with the reason) envelopes.
 */

import { beforeEach, describe, expect, it } from 'vitest'
import {
  apiJson,
  callWorker,
  expectSecurityHeaders,
  mockOrigin,
  readEnvelope,
  resetEdgeState,
} from './helpers'

beforeEach(resetEdgeState)

const encoder = new TextEncoder()

async function bytesOf(response: Response): Promise<number[]> {
  return [...new Uint8Array(await response.arrayBuffer())]
}

/** An analysis-shaped body with formatting a JSON round trip would not preserve. */
const RESULT_BODY =
  '{"spec":{"analysis":"estimate","years":[2024]},"estimate":1.2345678901234567e-7,' +
  '"se":0.10000000000000001,"big":1e400,"label":"caf\\u00e9 é ☕","nested":{ "k" : [1,2.50,-0.0] }}'

describe('API responses pass through byte for byte', () => {
  it.each([
    ['GET', '/api/v1/meta', undefined],
    ['GET', '/api/v1/activities?level=1', undefined],
    [
      'POST',
      '/api/v1/analysis/estimate',
      { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' },
    ],
    [
      'POST',
      '/api/v1/analysis/compare',
      { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' },
    ],
  ])('200 JSON: %s %s', async (_, path, init) => {
    mockOrigin(() => apiJson(RESULT_BODY, 200, { 'content-type': 'application/json' }))
    const response = await callWorker(path, init as RequestInit | undefined)
    expect(response.status).toBe(200)
    expect(response.headers.get('content-type')).toBe('application/json')
    expect(await bytesOf(response)).toEqual([...encoder.encode(RESULT_BODY)])
  })

  it('a large body is streamed through intact', async () => {
    const big = JSON.stringify({ activities: Array.from({ length: 3000 }, (_, i) => ({ i })) })
    mockOrigin(() => apiJson(big))
    const response = await callWorker('/api/v1/activities')
    expect(await response.text()).toBe(big)
  })

  it('304 Not Modified passes through without a body', async () => {
    mockOrigin(() => new Response(null, { status: 304, headers: { etag: '"v1"' } }))
    const response = await callWorker('/api/v1/meta', { headers: { 'if-none-match': '"v1"' } })
    expect(response.status).toBe(304)
    expect(response.headers.get('etag')).toBe('"v1"')
    expectSecurityHeaders(response)
  })
})

describe('API error envelopes pass through byte for byte', () => {
  const cases: [number, string, Record<string, string>][] = [
    [400, 'malformed_json', {}],
    [404, 'activity_not_found', {}],
    [413, 'payload_too_large', {}],
    [415, 'unsupported_media_type', {}],
    [422, 'validation_error', {}],
    [429, 'rate_limited', { 'retry-after': '17' }],
    [403, 'forbidden_origin', {}],
    [500, 'internal_error', {}],
    [503, 'overloaded', { 'retry-after': '5' }],
    [503, 'database_unavailable', { 'retry-after': '30' }],
    [503, 'analysis_timeout', { 'retry-after': '60' }],
    [503, 'data_version_mismatch', { 'retry-after': '30' }],
  ]

  it.each(cases)('%i %s', async (status, code, headers) => {
    const body =
      `{"error":{"code":"${code}","message":"Message with \\"quotes\\" and ünïcode.",` +
      `"details":{"retry_after_seconds":17,"scope":"client","errors":[{"location":["body","years",0]}]}}}`
    mockOrigin(() => apiJson(body, status, { 'x-request-id': 'origin-req-err', ...headers }))
    const response = await callWorker('/api/v1/analysis/estimate', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{"analysis":"estimate"}',
    })
    expect(response.status).toBe(status)
    expect(await bytesOf(response)).toEqual([...encoder.encode(body)])
    expect(response.headers.get('x-request-id')).toBe('origin-req-err')
    expect(response.headers.get('retry-after')).toBe(headers['retry-after'] ?? null)
    expect(response.headers.get('cache-control')).toBe('no-store')
    expectSecurityHeaders(response)
  })

  it('a minimal envelope with null details passes too', async () => {
    const body = '{"error":{"code":"not_found","message":"Not Found","details":null}}'
    mockOrigin(() => apiJson(body, 404))
    const response = await callWorker('/api/v1/meta')
    expect(await response.text()).toBe(body)
  })
})

describe('responses that are not the API are converted', () => {
  const waking: [string, () => Response][] = [
    [
      'Render hibernate 503 (empty)',
      () =>
        new Response('', {
          status: 503,
          headers: { 'x-render-routing': 'dynamic-hibernate-error-503' },
        }),
    ],
    [
      'hibernate header on a 200',
      () =>
        new Response('<html>', {
          status: 200,
          headers: { 'x-render-routing': 'dynamic-hibernate', 'content-type': 'text/html' },
        }),
    ],
    [
      'HTML loading page with 200',
      () =>
        new Response('<!doctype html><title>Waking up</title>', {
          headers: { 'content-type': 'text/html; charset=utf-8' },
        }),
    ],
    [
      'HTML 503',
      () =>
        new Response('<html>Service Unavailable</html>', {
          status: 503,
          headers: { 'content-type': 'text/html' },
        }),
    ],
    [
      'plain-text 503 from the platform',
      () => new Response('no healthy upstream', { status: 503 }),
    ],
    [
      'plain-text 503 of another media type',
      () =>
        new Response('Service Unavailable', {
          status: 503,
          headers: { 'content-type': 'text/html' },
        }),
    ],
    ['JSON 503 that is not an envelope', () => apiJson('{"status":"starting"}', 503)],
  ]

  it.each(waking)('%s -> 503 service_waking, Retry-After 5', async (_, make) => {
    mockOrigin(make)
    const response = await callWorker('/api/v1/meta')
    expect(response.status).toBe(503)
    expect(response.headers.get('retry-after')).toBe('5')
    expectSecurityHeaders(response)
    const error = await readEnvelope(response)
    expect(error.code).toBe('service_waking')
    expect(error.details).toEqual({ retry_after_seconds: 5 })
  })

  const unavailable: [string, () => Response, string][] = [
    ...[520, 521, 522, 523, 525, 526, 527, 530].map(
      (status): [string, () => Response, string] => [
        `Cloudflare ${status}`,
        () =>
          new Response('<html>cloudflare error</html>', {
            status,
            headers: { 'content-type': 'text/html' },
          }),
        'platform_error',
      ],
    ),
    [
      'Cloudflare 524: the API took the request and had not answered',
      () =>
        new Response('<html>a timeout occurred</html>', {
          status: 524,
          headers: { 'content-type': 'text/html' },
        }),
      'origin_timeout',
    ],
    [
      'Cloudflare 52x even with a JSON body',
      () => apiJson('{"error":{"code":"x","message":"y","details":null}}', 522),
      'platform_error',
    ],
    [
      'HTML 404 from the platform',
      () =>
        new Response('<html>Not Found</html>', {
          status: 404,
          headers: { 'content-type': 'text/html' },
        }),
      'platform_error',
    ],
    [
      'HTML 502',
      () => new Response('<html>Bad Gateway</html>', { status: 502 }),
      'platform_error',
    ],
    [
      'JSON 500 that is not an envelope',
      () => apiJson('{"detail":"Internal Server Error"}', 500),
      'platform_error',
    ],
    [
      'JSON 422 with a non-string code',
      () => apiJson('{"error":{"code":1,"message":"x"}}', 422),
      'platform_error',
    ],
    ['JSON 400 that is not JSON', () => apiJson('not json', 400), 'platform_error'],
    ['JSON 429 array', () => apiJson('[]', 429), 'platform_error'],
    [
      'text/plain 200',
      () => new Response('ok', { headers: { 'content-type': 'text/plain' } }),
      'platform_error',
    ],
    ['200 without a Content-Type', () => new Response('{}'), 'platform_error'],
    [
      'a redirect (never followed)',
      () =>
        new Response(null, {
          status: 302,
          headers: { location: 'https://elsewhere.example/' },
        }),
      'platform_error',
    ],
    [
      'an oversized JSON error body',
      () =>
        apiJson(
          `{"error":{"code":"x","message":"${'m'.repeat(300 * 1024)}","details":null}}`,
          422,
        ),
      'platform_error',
    ],
  ]

  it.each(unavailable)('%s -> 502 upstream_unavailable (%s)', async (_, make, reason) => {
    mockOrigin(make)
    const response = await callWorker('/api/v1/meta')
    expect(response.status).toBe(502)
    expect(response.headers.get('retry-after')).toBe('10')
    expect(response.headers.get('location')).toBeNull()
    expectSecurityHeaders(response)
    const error = await readEnvelope(response)
    expect(error.code).toBe('upstream_unavailable')
    expect(error.details).toEqual({ retry_after_seconds: 10, reason })
  })

  it("uvicorn's own connection-limit 503 -> 503 overloaded, Retry-After 10", async () => {
    // Exactly what tests/integration/api_prod/test_server.py records uvicorn sending.
    mockOrigin(
      () =>
        new Response('Service Unavailable', {
          status: 503,
          headers: { 'content-type': 'text/plain; charset=utf-8', connection: 'close' },
        }),
    )
    const response = await callWorker('/api/v1/analysis/estimate', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{"years":[2023]}',
    })
    expect(response.status).toBe(503)
    expect(response.headers.get('retry-after')).toBe('10')
    expectSecurityHeaders(response)
    const error = await readEnvelope(response)
    expect(error.code).toBe('overloaded')
    expect(error.details).toEqual({ retry_after_seconds: 10, scope: 'global' })
  })

  it('an error body cut off mid-stream -> 502 upstream_unavailable (broken_response)', async () => {
    mockOrigin(() => {
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new TextEncoder().encode('{"error":{"code":"x",'))
          controller.error(new TypeError('Network connection lost.'))
        },
      })
      return new Response(body, {
        status: 503,
        headers: { 'content-type': 'application/json' },
      })
    })
    const response = await callWorker('/api/v1/meta')
    expect(response.status).toBe(502)
    const error = await readEnvelope(response)
    expect(error.code).toBe('upstream_unavailable')
    expect(error.details).toEqual({ retry_after_seconds: 10, reason: 'broken_response' })
  })

  it('converted responses never leak the origin body or headers', async () => {
    mockOrigin(
      () =>
        new Response('<html>secret stack trace at /srv/app.py</html>', {
          status: 502,
          headers: { 'content-type': 'text/html', server: 'cloudflare', 'rndr-id': 'r-1' },
        }),
    )
    const response = await callWorker('/api/v1/meta')
    const text = await response.text()
    expect(text).not.toContain('stack trace')
    expect(response.headers.get('server')).toBeNull()
    expect(response.headers.get('rndr-id')).toBeNull()
  })
})
