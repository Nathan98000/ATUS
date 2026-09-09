import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'

import { errorResponse, server } from '../test/server'
import { ApiError, NetworkError, apiBaseUrl, apiRequest } from './client'

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

  it('maps a non-JSON error body to a malformed_response ApiError', async () => {
    server.use(
      http.get(
        `${apiBaseUrl()}/api/v1/meta`,
        () => new HttpResponse('<html>Bad gateway</html>', { status: 502 }),
      ),
    )
    const failure = (await apiRequest('/meta').catch((error: unknown) => error)) as ApiError
    expect(failure.code).toBe('malformed_response')
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
    const result = await apiRequest('/analysis/estimate', { method: 'POST', body: {} })
    expect(result.analysisKey).toBe('k')
    expect(result.cache).toBe('hit')
  })
})
