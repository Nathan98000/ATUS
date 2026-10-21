/**
 * The owner's pause switch (src/pause.ts; review round 2, brief §14,
 * docs/security.md R13): with `API_PAUSED_UNTIL` set, every /api route that
 * would reach Render answers 503 `api_paused` with the date, without calling
 * Render or the governor, while everything that never reaches Render keeps
 * working.
 */

import { beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { sha256Hex } from '../../src/http'
import {
  apiJson,
  callWorker,
  controlClock,
  expectSecurityHeaders,
  governorSql,
  mockOrigin,
  readEnvelope,
  resetEdgeState,
} from './helpers'

beforeEach(resetEdgeState)

const T = Date.UTC(2036, 9, 20, 12, 0, 0)
const UNTIL = Date.UTC(2036, 10, 1)
const PAUSED: Partial<Env> = {
  API_PAUSED_UNTIL: '2036-11-01',
  API_PAUSED_REASON: 'render_quota',
}
const DATA_VERSION = '0325:serving-20361020T000000Z-0a1b2c3d:0123456789ab'
const RELEASE = { DATA_VERSION, ANALYTICS_VERSION: '0.2' }
const BODY = '{"activity":{"preset":"sleep"},"years":[2023]}'
const visitor = { 'cf-connecting-ip': '203.0.113.7' }

function post(path = '/api/v1/analysis/estimate', body = BODY): [string, RequestInit] {
  return [
    path,
    { method: 'POST', headers: { ...visitor, 'content-type': 'application/json' }, body },
  ]
}

let snapshotId = ''

beforeAll(async () => {
  snapshotId = `0123456789ab-${(await sha256Hex(DATA_VERSION)).slice(0, 16)}`
})

/** A deployed snapshot that holds /api/v1/meta only. */
function withSnapshot(): Partial<Env> {
  const assets = {
    async fetch(input: RequestInfo | URL): Promise<Response> {
      const { pathname } = new URL(input instanceof Request ? input.url : String(input))
      if (pathname === `/snap/${snapshotId}/get/meta.json`) {
        return new Response('{"api_version":"v1"}', {
          headers: { 'content-type': 'application/json' },
        })
      }
      return new Response('<!doctype html>', { headers: { 'content-type': 'text/html' } })
    },
  }
  return { ...RELEASE, SNAPSHOT_ID: snapshotId, ASSETS: assets as unknown as Fetcher }
}

const ORIGIN_ROUTES: [label: string, request: [string, RequestInit]][] = [
  ['an uncached analysis', post()],
  [
    'a trend',
    post('/api/v1/analysis/trend', '{"activity":{"preset":"sleep"},"years":[2022,2023]}'),
  ],
  ['a metadata lookup (query string)', ['/api/v1/meta?check=1', { headers: visitor }]],
  ['a metadata lookup (unknown code)', ['/api/v1/activities/999999', { headers: visitor }]],
  ['the health check through to Render', ['/api/health', { headers: visitor }]],
  ['the prewake', ['/api/v1/wake', { headers: visitor }]],
]

describe('while the switch is on', () => {
  it.each(ORIGIN_ROUTES)(
    'answers %s with 503 api_paused and the date, without Render or the governor',
    async (_label, [path, init]) => {
      controlClock(T)
      const calls = mockOrigin(() => apiJson('{"from":"origin"}'))
      const response = await callWorker(path, init, { ...withSnapshot(), ...PAUSED })
      expect(response.status).toBe(503)
      expect(response.headers.get('retry-after')).toBe(String((UNTIL - T) / 1000))
      expectSecurityHeaders(response)
      const error = await readEnvelope(response)
      expect(error.code).toBe('api_paused')
      expect(error.details).toEqual({
        until: new Date(UNTIL).toISOString(),
        reason: 'render_quota',
        retry_after_seconds: (UNTIL - T) / 1000,
      })
      expect(error.message).toContain('2036-11-01')
      expect(calls).toHaveLength(0)
      expect(await governorSql('SELECT * FROM prefixes')).toEqual([])
      expect(await governorSql('SELECT * FROM leases')).toEqual([])
    },
  )

  it('keeps serving what never reaches Render: snapshot metadata and KV-cached analyses', async () => {
    controlClock(T)
    const calls = mockOrigin(() =>
      apiJson('{"estimate":1}', 200, {
        'x-cache': 'miss',
        'x-data-version': DATA_VERSION,
        'x-analytics-version': '0.2',
        'x-response-format': '1',
        'server-timing': 'db;dur=3',
      }),
    )
    // Computed and stored before the switch went on.
    expect((await callWorker(...post(), withSnapshot())).status).toBe(200)
    expect(calls).toHaveLength(1)

    const hit = await callWorker(...post(), { ...withSnapshot(), ...PAUSED })
    expect(hit.status).toBe(200)
    expect(hit.headers.get('x-cache')).toBe('edge')
    expect(await hit.text()).toBe('{"estimate":1}')
    const meta = await callWorker(
      '/api/v1/meta',
      { headers: visitor },
      {
        ...withSnapshot(),
        ...PAUSED,
      },
    )
    expect(meta.status).toBe(200)
    expect(await meta.text()).toBe('{"api_version":"v1"}')
    expect(calls).toHaveLength(1)
  })

  it('still answers request errors first: 413 and 415 are about the request', async () => {
    controlClock(T)
    mockOrigin(() => apiJson('{}'))
    const response = await callWorker(
      '/api/v1/analysis/estimate',
      { method: 'POST', headers: { 'content-type': 'text/plain' }, body: 'x' },
      PAUSED,
    )
    expect(response.status).toBe(415)
  })

  it('reports itself in /api/_edge/health, which stays 200', async () => {
    controlClock(T)
    const response = await callWorker('/api/_edge/health', undefined, PAUSED)
    expect(response.status).toBe(200)
    const body = (await response.json()) as { status: string; api_paused: unknown }
    expect(body.status).toBe('ok')
    expect(body.api_paused).toEqual({
      until: new Date(UNTIL).toISOString(),
      reason: 'render_quota',
    })
    const off = (await (await callWorker('/api/_edge/health')).json()) as {
      api_paused: unknown
    }
    expect(off.api_paused).toBeNull()
  })

  it('leaves the ops report readable', async () => {
    controlClock(T)
    const response = await callWorker(
      '/api/_ops/budget',
      { headers: { authorization: 'Bearer test-ops-token' } },
      PAUSED,
    )
    expect(response.status).toBe(200)
  })
})

describe('the switch values', () => {
  it.each([
    ['empty', ''],
    ['absent', undefined],
    ['a date already past', '2036-10-01'],
    ['a time already past', '2036-10-20T11:59:59Z'],
  ])('is off when %s', async (_label, until) => {
    controlClock(T)
    const calls = mockOrigin(() => apiJson('{"status":"ok"}'))
    const response = await callWorker('/api/health', undefined, {
      API_PAUSED_UNTIL: until,
      API_PAUSED_REASON: 'render_quota',
    })
    expect(response.status).toBe(200)
    expect(calls).toHaveLength(1)
  })

  it('takes a UTC time as well as a date', async () => {
    controlClock(T)
    mockOrigin(() => apiJson('{}'))
    const response = await callWorker('/api/health', undefined, {
      API_PAUSED_UNTIL: '2036-10-20T13:30:00Z',
      API_PAUSED_REASON: 'maintenance',
    })
    expect(response.status).toBe(503)
    expect(response.headers.get('retry-after')).toBe('5400')
    const error = await readEnvelope(response)
    expect(error.details).toMatchObject({
      until: '2036-10-20T13:30:00.000Z',
      reason: 'maintenance',
    })
    expect(error.message).toContain('maintenance')
  })

  it.each([
    ['unknown', 'unknown'],
    ['unreadable', 'next month'],
    ['a date that does not exist', '2036-02-30'],
  ])(
    'pauses without a date when the end is %s, retried after an hour',
    async (_label, until) => {
      controlClock(T)
      const calls = mockOrigin(() => apiJson('{}'))
      const response = await callWorker('/api/health', undefined, {
        API_PAUSED_UNTIL: until,
        API_PAUSED_REASON: 'render_suspended',
      })
      expect(response.status).toBe(503)
      expect(response.headers.get('retry-after')).toBe('3600')
      const error = await readEnvelope(response)
      expect(error.code).toBe('api_paused')
      expect(error.details).toEqual({
        until: null,
        reason: 'render_suspended',
        retry_after_seconds: 3600,
      })
      expect(calls).toHaveLength(0)
    },
  )

  it('reports an unknown reason as null', async () => {
    controlClock(T)
    mockOrigin(() => apiJson('{}'))
    const response = await callWorker('/api/health', undefined, {
      API_PAUSED_UNTIL: '2036-11-01',
      API_PAUSED_REASON: '<b>bogus</b>',
    })
    const error = await readEnvelope(response)
    expect(error.details).toMatchObject({ reason: null })
    expect(error.message).not.toContain('bogus')
  })
})
