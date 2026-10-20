import { HttpResponse, delay, http } from 'msw'
import { describe, expect, it } from 'vitest'

import { errorResponse, server } from '../test/server'
import { ApiError, NetworkError, apiBaseUrl, apiRequest, parseRetryAfter } from './client'

describe('apiRequest', () => {
  it('parses the API error envelope into an ApiError', async () => {
    server.use(
      http.get(`${apiBaseUrl()}/api/v1/meta`, () =>
        errorResponse(422, 'unsupported_analysis', 'TUFNWGTP is undefined for 2020…'),
      ),
    )
    const failure = await apiRequest('/meta').catch((error: unknown) => error)
    expect(failure).toBeInstanceOf(ApiError)
    const apiError = failure as ApiError
    expect(apiError.status).toBe(422)
    expect(apiError.code).toBe('unsupported_analysis')
    expect(apiError.message).toContain('TUFNWGTP')
    expect(apiError.requestId).toBe('test-request-id')
  })

  it('maps unreachable servers to NetworkError', async () => {
    server.use(http.get(`${apiBaseUrl()}/api/v1/meta`, () => HttpResponse.error()))
    await expect(apiRequest('/meta')).rejects.toBeInstanceOf(NetworkError)
  })

  it('maps a non-JSON 5xx page to upstream_unavailable', async () => {
    // Intentional Phase 5 change (audit F13): a host or proxy error page is an
    // upstream outage, not a malformed API response.
    server.use(
      http.get(
        `${apiBaseUrl()}/api/v1/meta`,
        () => new HttpResponse('<html>Bad gateway</html>', { status: 502 }),
      ),
    )
    const failure = (await apiRequest('/meta').catch((error: unknown) => error)) as ApiError
    expect(failure.code).toBe('upstream_unavailable')
    expect(failure.status).toBe(502)
  })

  it('maps a JSON body that is not the envelope to malformed_response', async () => {
    server.use(
      http.get(`${apiBaseUrl()}/api/v1/meta`, () =>
        HttpResponse.json({ detail: 'not the envelope' }, { status: 500 }),
      ),
    )
    const failure = (await apiRequest('/meta').catch((error: unknown) => error)) as ApiError
    expect(failure.code).toBe('malformed_response')
  })

  it('exposes analysis headers from successful responses', async () => {
    server.use(
      http.post(`${apiBaseUrl()}/api/v1/analysis/estimate`, () =>
        HttpResponse.json(
          { ok: true },
          { headers: { 'X-Analysis-Key': 'k', 'X-Cache': 'hit' } },
        ),
      ),
    )
    const result = await apiRequest('/analysis/estimate', { method: 'POST', jsonBody: '{}' })
    expect(result.analysisKey).toBe('k')
    expect(result.cache).toBe('hit')
  })
})

describe('operational responses (Phase 5)', () => {
  it('maps a non-JSON 429 on /api to edge_daily_limit', async () => {
    server.use(
      http.get(
        '/api/v1/meta',
        () =>
          new HttpResponse('<html>Error 1027</html>', {
            status: 429,
            headers: { 'Content-Type': 'text/html' },
          }),
      ),
    )
    const failure = (await apiRequest('/meta').catch((error: unknown) => error)) as ApiError
    expect(failure).toBeInstanceOf(ApiError)
    expect(failure.code).toBe('edge_daily_limit')
    expect(failure.status).toBe(429)
  })

  it('keeps a JSON 429 envelope as rate_limited with its Retry-After', async () => {
    server.use(
      http.post('/api/v1/analysis/estimate', () =>
        HttpResponse.json(
          {
            error: {
              code: 'rate_limited',
              message: 'Slow down.',
              details: { scope: 'client' },
            },
          },
          { status: 429, headers: { 'Retry-After': '17' } },
        ),
      ),
    )
    const failure = (await apiRequest('/analysis/estimate', {
      method: 'POST',
      jsonBody: '{}',
    }).catch((error: unknown) => error)) as ApiError
    expect(failure.code).toBe('rate_limited')
    expect(failure.retryAfterSeconds).toBe(17)
  })

  it('falls back to details.retry_after_seconds when the header is absent', async () => {
    server.use(
      http.get('/api/v1/meta', () =>
        HttpResponse.json(
          {
            error: {
              code: 'overloaded',
              message: 'Busy.',
              details: { retry_after_seconds: 4.2 },
            },
          },
          { status: 503 },
        ),
      ),
    )
    const failure = (await apiRequest('/meta').catch((error: unknown) => error)) as ApiError
    expect(failure.retryAfterSeconds).toBe(5)
  })

  it('maps an empty 503 from a sleeping host to upstream_unavailable', async () => {
    server.use(http.get('/api/v1/meta', () => new HttpResponse(null, { status: 503 })))
    const failure = (await apiRequest('/meta').catch((error: unknown) => error)) as ApiError
    expect(failure.code).toBe('upstream_unavailable')
  })

  it('reports X-Data-Version on successful responses', async () => {
    server.use(
      http.get('/api/v1/meta', () =>
        HttpResponse.json(
          { ok: true },
          { headers: { 'X-Data-Version': '0325:serving-x:abc' } },
        ),
      ),
    )
    const result = await apiRequest('/meta')
    expect(result.dataVersion).toBe('0325:serving-x:abc')
  })

  it('sends the serialized body byte for byte', async () => {
    let received = ''
    server.use(
      http.post('/api/v1/analysis/trend', async ({ request }) => {
        received = await request.text()
        return HttpResponse.json({ ok: true })
      }),
    )
    const body = '{"activity":{"preset":"sleep"},"years":[2024,2025]}'
    await apiRequest('/analysis/trend', { method: 'POST', jsonBody: body })
    expect(received).toBe(body)
  })

  it('gives up after timeoutMs with a timed-out NetworkError', async () => {
    server.use(
      http.get('/api/v1/meta', async () => {
        await delay(500)
        return HttpResponse.json({ ok: true })
      }),
    )
    const failure = (await apiRequest('/meta', { timeoutMs: 20 }).catch(
      (error: unknown) => error,
    )) as NetworkError
    expect(failure).toBeInstanceOf(NetworkError)
    expect(failure.timedOut).toBe(true)
  })

  it("passes the caller's own abort through untouched", async () => {
    server.use(
      http.get('/api/v1/meta', async () => {
        await delay(500)
        return HttpResponse.json({ ok: true })
      }),
    )
    const controller = new AbortController()
    const pending = apiRequest('/meta', { signal: controller.signal, timeoutMs: 10_000 })
    controller.abort()
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
  })
})

describe('parseRetryAfter', () => {
  it('reads delta-seconds and HTTP dates, and ignores junk', () => {
    const now = Date.UTC(2026, 9, 20, 12, 0, 0)
    expect(parseRetryAfter('30', now)).toBe(30)
    expect(parseRetryAfter(' 0 ', now)).toBe(0)
    expect(parseRetryAfter('Tue, 20 Oct 2026 12:01:30 GMT', now)).toBe(90)
    expect(parseRetryAfter('Tue, 20 Oct 2026 11:00:00 GMT', now)).toBe(0)
    expect(parseRetryAfter('soon', now)).toBeNull()
    expect(parseRetryAfter(null, now)).toBeNull()
    expect(parseRetryAfter('999999999', now)).toBe(31 * 24 * 60 * 60)
  })
})
