/**
 * GET /api/_ops/budget (src/ops.ts): the governor's state behind the ops
 * bearer token, indistinguishable from an unknown route without it.
 */

import { beforeEach, describe, expect, it } from 'vitest'
import { prefixHmac } from '../../src/admission'
import {
  apiJson,
  callWorker,
  controlClock,
  expectSecurityHeaders,
  mockOrigin,
  readEnvelope,
  resetEdgeState,
} from './helpers'

beforeEach(resetEdgeState)

const OPS = '/api/_ops/budget'
const TOKEN = 'test-ops-token'
const T = Date.UTC(2036, 9, 20, 12, 0, 0)

function withAuth(authorization: string, method = 'GET'): RequestInit {
  return { method, headers: { authorization } }
}

/** The body of a 404 envelope without its per-request ID. */
async function notFoundBody(response: Response): Promise<string> {
  expect(response.status).toBe(404)
  expectSecurityHeaders(response)
  return JSON.stringify(await readEnvelope(response))
}

describe('without a valid bearer token it does not exist', () => {
  it.each([
    ['no Authorization header', undefined],
    ['a wrong token', withAuth('Bearer wrong-token')],
    ['a token prefix', withAuth('Bearer test-ops')],
    ['a token with a suffix', withAuth(`Bearer ${TOKEN}x`)],
    ['the Basic scheme', withAuth(`Basic ${TOKEN}`)],
    ['the bare token', withAuth(TOKEN)],
    ['an empty bearer', withAuth('Bearer ')],
    ['two tokens', withAuth(`Bearer ${TOKEN} ${TOKEN}`)],
    ['the right token on POST', withAuth(`Bearer ${TOKEN}`, 'POST')],
    ['the right token on HEAD', withAuth(`Bearer ${TOKEN}`, 'HEAD')],
  ])('%s -> the unknown-route 404', async (_label, init) => {
    const calls = mockOrigin(() => apiJson('{}'))
    const unknown = await notFoundBody(await callWorker('/api/_ops/nothing'))
    const response = await callWorker(OPS, init)
    if (init?.method === 'HEAD') {
      expect(response.status).toBe(404)
    } else {
      expect(await notFoundBody(response)).toBe(unknown)
    }
    expect(calls).toHaveLength(0)
  })

  it('stays hidden when OPS_TOKEN is not configured', async () => {
    const response = await callWorker(OPS, withAuth('Bearer '), { OPS_TOKEN: '' })
    expect(response.status).toBe(404)
  })
})

describe('with the bearer token', () => {
  it('reports the edge versions and the governor budgets', async () => {
    controlClock(T)
    const calls = mockOrigin(() => apiJson('{}'))
    const response = await callWorker(OPS, withAuth(`bearer ${TOKEN}`), {
      DATA_VERSION: '0325:serving-20361020T000000Z-0a1b2c3d:0123456789ab',
      ANALYTICS_VERSION: '0.2',
    })
    expect(response.status).toBe(200)
    expectSecurityHeaders(response)
    expect(calls).toHaveLength(0)
    const payload = (await response.json()) as {
      status: string
      edge: Record<string, unknown>
      governor: Record<string, unknown>
    }
    expect(payload.status).toBe('ok')
    expect(payload.edge).toEqual({
      api_version: 'v1',
      analytics_version: '0.2',
      data_version: '0325:serving-20361020T000000Z-0a1b2c3d:0123456789ab',
      response_format: '1',
      snapshot_id: null,
      cache_scope: '10fe49ed95dd1929',
    })
    expect(payload.governor).toMatchObject({
      day: '2036-10-20',
      mode: 'conservative',
      neon_configured: false,
      analyses: { admitted: 0, limit: 600, heavy_limit: 300 },
      prefixes: { active: 0, limit: 60 },
    })
  })

  it('contains no client address and no prefix hash after traffic', async () => {
    controlClock(T)
    mockOrigin(() => apiJson('{"ok":true}', 200, { 'x-cache': 'miss' }))
    await callWorker('/api/v1/analysis/estimate', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'cf-connecting-ip': '203.0.113.7' },
      body: '{"years":[2024]}',
    })
    const response = await callWorker(OPS, withAuth(`Bearer ${TOKEN}`))
    const text = await response.text()
    expect(text).toContain('"admitted":1')
    expect(text).not.toContain('203.0.113')
    expect(text).not.toContain(await prefixHmac('test-ip-hmac-key', T, '203.0.113.0/24'))
  })

  it('answers 503 capacity_unavailable when the governor cannot be reached', async () => {
    const governor = {
      idFromName: () => ({}),
      get: () => ({ budget: () => Promise.reject(new Error('Durable Object reset')) }),
    } as unknown as Env['GOVERNOR']
    const response = await callWorker(OPS, withAuth(`Bearer ${TOKEN}`), { GOVERNOR: governor })
    expect(response.status).toBe(503)
    expect((await readEnvelope(response)).code).toBe('capacity_unavailable')
  })
})
