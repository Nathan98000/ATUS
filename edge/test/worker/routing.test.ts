/** The /api allowlist (ADR-011 step 3.1) and the static-asset fallthrough. */

import { createExecutionContext } from 'cloudflare:test'
import { env } from 'cloudflare:workers'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import worker from '../../src/index'
import {
  BASE,
  apiJson,
  callWorker,
  expectSecurityHeaders,
  mockOrigin,
  readEnvelope,
  resetEdgeState,
} from './helpers'

const ORIGIN = 'https://origin.invalid'
const JSON_POST = {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: '{}',
}

beforeEach(resetEdgeState)

describe('allowlisted routes reach the origin', () => {
  const cases: [string, RequestInit | undefined, string, string][] = [
    ['/api/v1/meta', undefined, 'GET', `${ORIGIN}/api/v1/meta`],
    ['/api/v1/activities', undefined, 'GET', `${ORIGIN}/api/v1/activities`],
    ['/api/v1/activities/presets', undefined, 'GET', `${ORIGIN}/api/v1/activities/presets`],
    ['/api/v1/activities/0101', undefined, 'GET', `${ORIGIN}/api/v1/activities/0101`],
    ['/api/v1/activities/18', undefined, 'GET', `${ORIGIN}/api/v1/activities/18`],
    ['/api/v1/activities/110101', undefined, 'GET', `${ORIGIN}/api/v1/activities/110101`],
    ['/api/v1/population/metadata', undefined, 'GET', `${ORIGIN}/api/v1/population/metadata`],
    ['/api/v1/analysis/estimate', JSON_POST, 'POST', `${ORIGIN}/api/v1/analysis/estimate`],
    ['/api/v1/analysis/trend', JSON_POST, 'POST', `${ORIGIN}/api/v1/analysis/trend`],
    ['/api/v1/analysis/compare', JSON_POST, 'POST', `${ORIGIN}/api/v1/analysis/compare`],
    ['/api/health', undefined, 'GET', `${ORIGIN}/health`],
  ]

  it.each(cases)('%s', async (path, init, method, upstream) => {
    const calls = mockOrigin(() => apiJson('{"ok":true}'))
    const response = await callWorker(path, init)
    expect(response.status).toBe(200)
    expect(calls).toHaveLength(1)
    expect(calls[0]!.url).toBe(upstream)
    expect(calls[0]!.method).toBe(method)
  })

  it('forwards the raw query string on GET proxies', async () => {
    const calls = mockOrigin(() => apiJson('{"activities":[],"total":0}'))
    await callWorker('/api/v1/activities?level=2&parent=01&search=sl%C3%A9ep')
    expect(calls[0]!.url).toBe(
      `${ORIGIN}/api/v1/activities?level=2&parent=01&search=sl%C3%A9ep`,
    )
  })

  it('does not forward a query string to origin /health', async () => {
    const calls = mockOrigin(() => apiJson('{"status":"ok"}'))
    await callWorker('/api/health?probe=1')
    expect(calls[0]!.url).toBe(`${ORIGIN}/health`)
  })

  it('forwards an activity code the API itself will reject', async () => {
    const envelope =
      '{"error":{"code":"activity_not_found","message":"Activity code \'abc\' does not exist in the 2003-25 lexicon.","details":{"code":"abc"}}}'
    const calls = mockOrigin(() => apiJson(envelope, 404))
    const response = await callWorker('/api/v1/activities/abc')
    expect(calls[0]!.url).toBe(`${ORIGIN}/api/v1/activities/abc`)
    expect(response.status).toBe(404)
    expect(await response.text()).toBe(envelope)
  })
})

describe('everything else under /api is a 404 without an origin call', () => {
  const paths = [
    '/api',
    '/api/',
    '/api/v1',
    '/api/v1/',
    '/api/v2/meta',
    '/api/V1/meta',
    '/api/v1/meta/',
    '/api/v1/metadata',
    '/api/ready',
    '/api/v1/ready',
    '/api/docs',
    '/api/redoc',
    '/api/openapi.json',
    '/api/v1/openapi.json',
    '/api/metrics',
    '/api/v1/analysis',
    '/api/v1/analysis/estimate/extra',
    '/api/v1/activities/01/02',
    '/api/v1/activities/..%2F..%2Fready',
    '/api/v1/activities/%30%31',
    '/api/v1/activities/' + '1'.repeat(33),
    '/api/_edge',
    '/api/_ops/budget',
    '/api/health/',
  ]

  it.each(paths)('%s', async (path) => {
    const calls = mockOrigin(() => apiJson('{}'))
    const response = await callWorker(path)
    expect(response.status).toBe(404)
    expect(calls).toHaveLength(0)
    expectSecurityHeaders(response)
    const error = await readEnvelope(response)
    expect(error.code).toBe('not_found')
    expect(error.details).toBeNull()
    expect(response.headers.get('x-request-id')).toMatch(/^[0-9a-f]{32}$/)
  })

  it('normalizes dot segments before matching (never reaching /ready or /docs)', async () => {
    const calls = mockOrigin(() => apiJson('{}'))
    const response = await callWorker('/api/v1/../../ready')
    // The URL parser resolves this to /ready, which is not an /api path.
    expect(calls).toHaveLength(0)
    expect(response.status).not.toBe(200)
  })
})

describe('wrong methods are a 405 without an origin call', () => {
  const cases: [string, string, string][] = [
    ['/api/v1/meta', 'POST', 'GET'],
    ['/api/v1/meta', 'HEAD', 'GET'],
    ['/api/v1/meta', 'OPTIONS', 'GET'],
    ['/api/v1/meta', 'DELETE', 'GET'],
    ['/api/v1/activities/0101', 'PUT', 'GET'],
    ['/api/v1/analysis/estimate', 'GET', 'POST'],
    ['/api/v1/analysis/trend', 'PATCH', 'POST'],
    ['/api/health', 'POST', 'GET'],
    ['/api/v1/wake', 'POST', 'GET'],
    ['/api/_edge/health', 'POST', 'GET'],
  ]

  it.each(cases)('%s %s', async (path, method, allowed) => {
    const calls = mockOrigin(() => apiJson('{}'))
    const init: RequestInit =
      method === 'GET' || method === 'HEAD'
        ? { method }
        : { method, headers: { 'content-type': 'application/json' }, body: '{}' }
    const response = await callWorker(path, init)
    expect(response.status).toBe(405)
    expect(response.headers.get('allow')).toBe(allowed)
    expect(calls).toHaveLength(0)
    expectSecurityHeaders(response)
    if (method !== 'HEAD') {
      const error = await readEnvelope(response)
      expect(error.code).toBe('method_not_allowed')
      expect(error.details).toEqual({ allowed: [allowed] })
    }
  })
})

describe('non-/api paths go to the static assets', () => {
  it.each(['/', '/explore', '/analysis/abc', '/apix', '/api-docs', '/snap/x/manifest.json'])(
    '%s',
    async (path) => {
      const calls = mockOrigin(() => apiJson('{}'))
      const assets = vi.fn(async () => new Response('<!doctype html>spa'))
      const response = await callWorker(path, undefined, {
        ASSETS: { fetch: assets } as unknown as Fetcher,
      })
      expect(assets).toHaveBeenCalledOnce()
      expect(await response.text()).toBe('<!doctype html>spa')
      expect(calls).toHaveLength(0)
    },
  )
})

describe('GET /api/_edge/health', () => {
  it('is answered by the Worker without any origin call', async () => {
    const calls = mockOrigin(() => apiJson('{}'))
    const response = await callWorker('/api/_edge/health', undefined, {
      DATA_VERSION: '2025:serving-20261020T000000Z-0a1b2c3d:0123456789ab',
      ANALYTICS_VERSION: '0.2',
      SNAPSHOT_ID: 'abcdef012345-0123456789abcdef',
    })
    expect(calls).toHaveLength(0)
    expect(response.status).toBe(200)
    expectSecurityHeaders(response)
    expect(await response.json()).toEqual({
      status: 'ok',
      edge_version: '0.1.0',
      api_version: 'v1',
      analytics_version: '0.2',
      data_version: '2025:serving-20261020T000000Z-0a1b2c3d:0123456789ab',
      response_format: '1',
      snapshot_id: 'abcdef012345-0123456789abcdef',
    })
  })

  it('works even when the origin is not configured', async () => {
    const calls = mockOrigin(() => apiJson('{}'))
    const response = await callWorker('/api/_edge/health', undefined, { RENDER_ORIGIN: '' })
    expect(response.status).toBe(200)
    expect(calls).toHaveLength(0)
    const body = (await response.json()) as Record<string, unknown>
    expect(body.data_version).toBeNull()
    expect(body.snapshot_id).toBeNull()
  })
})

describe('unexpected failures', () => {
  it('become a sanitized 500 internal_error envelope', async () => {
    const broken = new Proxy(env, {
      get(target, property) {
        if (property === 'PREWAKE') throw new Error('boom at /srv/app/secret.py')
        return Reflect.get(target, property)
      },
    })
    const response = await worker.fetch(
      new Request(`${BASE}/api/v1/wake`) as Request<unknown, IncomingRequestCfProperties>,
      broken,
      createExecutionContext(),
    )
    expect(response.status).toBe(500)
    expectSecurityHeaders(response)
    const text = await response.clone().text()
    expect(text).not.toContain('boom')
    expect(text).not.toContain('/srv')
    expect((await readEnvelope(response)).code).toBe('internal_error')
  })
})

describe('origin configuration', () => {
  it.each([
    ['unset', ''],
    ['not a URL', 'atus-api.onrender.com'],
    ['plain HTTP to a public host', 'http://atus-api.onrender.com'],
    ['credentials in the URL', 'https://user:pass@atus-api.onrender.com'],
    ['another scheme', 'ftp://atus-api.onrender.com'],
  ])('RENDER_ORIGIN %s gives 502 upstream_unavailable without a call', async (_, origin) => {
    const calls = mockOrigin(() => apiJson('{}'))
    const response = await callWorker('/api/v1/meta', undefined, { RENDER_ORIGIN: origin })
    expect(response.status).toBe(502)
    expect(calls).toHaveLength(0)
    expect((await readEnvelope(response)).code).toBe('upstream_unavailable')
  })

  it('a missing edge secret gives 502 without a call (never an unauthenticated request)', async () => {
    const calls = mockOrigin(() => apiJson('{}'))
    const response = await callWorker('/api/v1/meta', undefined, { EDGE_AUTH_SECRET: '' })
    expect(response.status).toBe(502)
    expect(calls).toHaveLength(0)
  })

  it('accepts a loopback HTTP origin for local development', async () => {
    const calls = mockOrigin(() => apiJson('{}'))
    await callWorker('/api/v1/meta', undefined, { RENDER_ORIGIN: 'http://127.0.0.1:8571/' })
    expect(calls[0]!.url).toBe('http://127.0.0.1:8571/api/v1/meta')
  })

  it('ignores any path on RENDER_ORIGIN', async () => {
    const calls = mockOrigin(() => apiJson('{}'))
    await callWorker('/api/v1/meta', undefined, {
      RENDER_ORIGIN: 'https://atus.example/prefix/',
    })
    expect(calls[0]!.url).toBe('https://atus.example/api/v1/meta')
  })
})
