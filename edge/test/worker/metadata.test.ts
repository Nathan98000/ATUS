/**
 * Metadata GETs (ADR-015 tier L0, answered by the Worker): the deployed
 * static snapshot answers them byte for byte with no origin or governor
 * call; what it cannot answer (a query string, a code it does not hold, no
 * snapshot deployed) reaches Render only as a governed metadata lookup, so
 * one client cannot spend Render's monthly outbound bandwidth.
 */

import { beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { prefixHmac } from '../../src/admission'
import { nextUtcMidnight, utcDay } from '../../src/budget'
import {
  DAILY_CU_HOURS_CONSERVATIVE,
  GLOBAL_DAILY_LOOKUPS,
  PREFIX_DAILY_LOOKUPS,
} from '../../src/constants'
import { sha256Hex } from '../../src/http'
import {
  apiJson,
  callWorker,
  controlClock,
  expectSecurityHeaders,
  governorSql,
  governorStub,
  mockOrigin,
  readEnvelope,
  resetEdgeState,
} from './helpers'

beforeEach(resetEdgeState)

const T = Date.UTC(2036, 9, 20, 12, 0, 0)
const MIDNIGHT = nextUtcMidnight(T)
const IP = '203.0.113.7'
const DATA_VERSION = '0325:serving-20361020T000000Z-0a1b2c3d:0123456789ab'
const ANALYTICS_VERSION = '0.2'
/** Bytes a JSON round trip would change: the snapshot's must reach the client unchanged. */
const FILES: Record<string, string> = {
  'get/meta.json': '{"api_version":"v1","analytics_version":"0.2", "x":1e400}',
  'get/activities.json': '{"activities":[{"code":"01","name":"Personal care"}],"total":1}',
  'get/activities/presets.json': '{"presets":[]}',
  'get/population/metadata.json': '{"dimensions":[]}',
  'get/activities/0101.json': '{"code":"0101","name":"Sleeping"}',
}
const ROUTES: [path: string, file: string, versioned: boolean][] = [
  ['/api/v1/meta', 'get/meta.json', true],
  ['/api/v1/activities', 'get/activities.json', true],
  ['/api/v1/activities/presets', 'get/activities/presets.json', false],
  ['/api/v1/population/metadata', 'get/population/metadata.json', false],
  ['/api/v1/activities/0101', 'get/activities/0101.json', true],
]

let snapshotId = ''

beforeAll(async () => {
  snapshotId = `0123456789ab-${(await sha256Hex(DATA_VERSION)).slice(0, 16)}`
})

/** The ASSETS binding with a deployed snapshot; a missing file gets the SPA's index.html. */
function snapshotAssets(id = snapshotId): { ASSETS: Fetcher; requested: string[] } {
  const requested: string[] = []
  const fetcher = {
    async fetch(input: RequestInfo | URL): Promise<Response> {
      const { pathname } = new URL(input instanceof Request ? input.url : String(input))
      requested.push(pathname)
      const prefix = `/snap/${id}/`
      const body = pathname.startsWith(prefix)
        ? FILES[pathname.slice(prefix.length)]
        : undefined
      if (body === undefined) {
        return new Response('<!doctype html><title>ATUS</title>', {
          headers: { 'content-type': 'text/html; charset=utf-8' },
        })
      }
      return new Response(body, { headers: { 'content-type': 'application/json' } })
    },
  }
  return { ASSETS: fetcher as unknown as Fetcher, requested }
}

function deployed(assets = snapshotAssets()): Partial<Env> {
  return {
    ASSETS: assets.ASSETS,
    SNAPSHOT_ID: snapshotId,
    DATA_VERSION,
    ANALYTICS_VERSION,
  }
}

const visitor = { headers: { 'cf-connecting-ip': IP } }

async function lookups(): Promise<{ day: number; prefix: number }> {
  const days = await governorSql<{ lookups: number }>(
    'SELECT lookups FROM days WHERE day = ?',
    utcDay(T),
  )
  const prefixes = await governorSql<{ lookups: number }>('SELECT lookups FROM prefixes')
  return {
    day: days[0]?.lookups ?? 0,
    prefix: prefixes.reduce((sum, row) => sum + row.lookups, 0),
  }
}

describe('metadata from the static snapshot', () => {
  it.each(ROUTES)(
    'answers %s from the snapshot, byte for byte, without the origin or the governor',
    async (path, file, versioned) => {
      controlClock(T)
      const calls = mockOrigin(() => apiJson('{"from":"origin"}'))
      const assets = snapshotAssets()
      const response = await callWorker(path, visitor, deployed(assets))
      expect(response.status).toBe(200)
      expect(await response.text()).toBe(FILES[file])
      expect(response.headers.get('content-type')).toBe('application/json')
      expect(response.headers.get('x-data-version')).toBe(versioned ? DATA_VERSION : null)
      expect(response.headers.get('x-analytics-version')).toBe(
        path === '/api/v1/meta' ? ANALYTICS_VERSION : null,
      )
      expect(response.headers.get('x-request-id')).toMatch(/^[0-9a-f]{32}$/)
      expectSecurityHeaders(response)
      expect(assets.requested).toEqual([`/snap/${snapshotId}/${file}`])
      expect(calls).toHaveLength(0)
      expect(await governorSql('SELECT * FROM prefixes')).toEqual([])
    },
  )

  it('still answers from the snapshot when the governor is down', async () => {
    const calls = mockOrigin(() => apiJson('{}'))
    const broken = {
      idFromName: () => ({}),
      get: () => ({ admitLookup: () => Promise.reject(new Error('Durable Object reset')) }),
    } as unknown as Env['GOVERNOR']
    const response = await callWorker('/api/v1/activities', visitor, {
      ...deployed(),
      GOVERNOR: broken,
    })
    expect(response.status).toBe(200)
    expect(calls).toHaveLength(0)
  })
})

describe('metadata lookups the snapshot cannot answer', () => {
  it.each([
    ['a query string', '/api/v1/activities?search=sleep', true],
    ['a code the snapshot does not hold', '/api/v1/activities/999999', true],
    ['a code that is not a lexicon code', '/api/v1/activities/x', true],
    ['no snapshot deployed', '/api/v1/meta', false],
  ])('reach the origin as a governed lookup for %s', async (_label, path, withSnapshot) => {
    controlClock(T)
    const calls = mockOrigin(() => apiJson('{"from":"origin"}'))
    const response = await callWorker(path, visitor, withSnapshot ? deployed() : {})
    expect(await response.text()).toBe('{"from":"origin"}')
    expect(calls).toHaveLength(1)
    expect(calls[0]!.url).toBe(`https://origin.invalid${path}`)
    expect(await lookups()).toEqual({ day: 1, prefix: 1 })
  })

  it('do not trust a snapshot whose id does not name the deployed data version', async () => {
    controlClock(T)
    const calls = mockOrigin(() => apiJson('{"from":"origin"}'))
    const other = `0123456789ab-${'0'.repeat(16)}`
    const response = await callWorker('/api/v1/meta', visitor, {
      ...deployed(snapshotAssets(other)),
      SNAPSHOT_ID: other,
    })
    expect(await response.text()).toBe('{"from":"origin"}')
    expect(calls).toHaveLength(1)
  })

  it('let one client replaying metadata reach the origin at most its daily lookups', async () => {
    // The finding: 300 GETs of /api/v1/activities?n=<i> from one address all
    // reached Render (~9 KB of outbound each, gzipped); at the Worker's 100k
    // requests a day Render's 5 GB a month would last under a week.
    controlClock(T)
    const calls = mockOrigin(() => apiJson('{"activities":[]}'))
    const statuses: number[] = []
    for (let i = 0; i < PREFIX_DAILY_LOOKUPS + 10; i += 1) {
      statuses.push((await callWorker(`/api/v1/activities?n=${i}`, visitor, deployed())).status)
    }
    expect(calls).toHaveLength(PREFIX_DAILY_LOOKUPS)
    expect(statuses.filter((status) => status === 429)).toHaveLength(10)
    // The snapshot still answers that client.
    expect((await callWorker('/api/v1/activities', visitor, deployed())).status).toBe(200)
    expect(calls).toHaveLength(PREFIX_DAILY_LOOKUPS)
  })

  it('refuse a network past its daily lookups with 429, without an origin call', async () => {
    controlClock(T)
    await governorSql(
      'INSERT INTO prefixes (day, prefix, count, attempts, lookups) VALUES (?, ?, 0, 0, ?)',
      utcDay(T),
      await prefixHmac('test-ip-hmac-key', T, '203.0.113.0/24'),
      PREFIX_DAILY_LOOKUPS,
    )
    const calls = mockOrigin(() => apiJson('{}'))
    const response = await callWorker('/api/v1/meta?x=1', visitor, deployed())
    expect(calls).toHaveLength(0)
    expect(response.status).toBe(429)
    expect(response.headers.get('retry-after')).toBe(String((MIDNIGHT - T) / 1000))
    expectSecurityHeaders(response)
    const error = await readEnvelope(response)
    expect(error.code).toBe('rate_limited')
    expect(error.details).toMatchObject({ scope: 'client', budget: 'metadata' })
    // Another network still gets its lookup.
    mockOrigin(() => apiJson('{}'))
    const other = await callWorker('/api/v1/meta?x=1', {
      headers: { 'cf-connecting-ip': '198.51.100.1' },
    })
    expect(other.status).toBe(200)
  })

  it('refuse everyone past the global daily lookups with 503, without an origin call', async () => {
    controlClock(T)
    await governorSql(
      'INSERT INTO days (day, lookups) VALUES (?, ?)',
      utcDay(T),
      GLOBAL_DAILY_LOOKUPS,
    )
    const calls = mockOrigin(() => apiJson('{}'))
    const response = await callWorker('/api/v1/activities/999999', visitor, deployed())
    expect(calls).toHaveLength(0)
    expect(response.status).toBe(503)
    const error = await readEnvelope(response)
    expect(error.code).toBe('daily_capacity_reached')
    expect(error.details).toMatchObject({ scope: 'global', budget: 'metadata' })
    const report = await governorStub().budget()
    expect(report.metadata).toMatchObject({
      origin_calls: GLOBAL_DAILY_LOOKUPS,
      limit: GLOBAL_DAILY_LOOKUPS,
      prefix_limit: PREFIX_DAILY_LOOKUPS,
    })
    expect(report.utilization.metadata).toBe(1)
  })

  it('charge a lookup that woke the database, so a network cannot keep Neon awake for free', async () => {
    // Review round 2 (lookup-wakes-neon-uncharged): one lookup after each
    // Render spin-down (16 minutes apart) runs a fresh API process's snapshot
    // read against Neon (db;dur > 0). Before, 89 such lookups were all
    // admitted and the governor charged 0 CU-h. Now each is charged its wake
    // session (2 CU x 300 s), and the network's lookups stop at its share of
    // the day's compute, as its analyses would.
    const clock = controlClock(T)
    const calls = mockOrigin(() =>
      apiJson('{"api_version":"v1"}', 200, { 'server-timing': 'db;dur=48.2, app;dur=1.0' }),
    )
    const statuses: number[] = []
    for (let i = 0; i < 30; i += 1) {
      const response = await callWorker(`/api/v1/meta?n=${i}`, visitor, deployed())
      statuses.push(response.status)
      if (response.status === 429) {
        const error = await readEnvelope(response)
        expect(error.code).toBe('rate_limited')
        expect(error.details).toMatchObject({ scope: 'client', budget: 'prefix' })
        expect(error.message).toContain('metadata')
      }
      clock.advance(16 * 60 * 1000)
    }
    expect(calls).toHaveLength(3)
    expect(statuses.slice(0, 3)).toEqual([200, 200, 200])
    expect(statuses.slice(3).every((status) => status === 429)).toBe(true)
    const report = await governorStub().budget()
    expect(report.compute_today.settled_cu_hours).toBeCloseTo((3 * 600) / 3600, 4)
    expect(report.prefixes.max_cu_hours).toBeCloseTo((3 * 600) / 3600, 4)
    expect(report.metadata.origin_calls).toBe(3)
    expect(await governorSql('SELECT id FROM leases')).toEqual([])
  })

  it('charge nothing for lookups a warm API answers from memory (db;dur=0)', async () => {
    const clock = controlClock(T)
    const calls = mockOrigin(() =>
      apiJson('{"activities":[]}', 200, { 'server-timing': 'db;dur=0, app;dur=0.4' }),
    )
    for (let i = 0; i < 30; i += 1) {
      expect((await callWorker(`/api/v1/activities?n=${i}`, visitor, deployed())).status).toBe(
        200,
      )
      clock.advance(16 * 60 * 1000)
    }
    expect(calls).toHaveLength(30)
    const report = await governorStub().budget()
    expect(report.compute_today.settled_cu_hours).toBe(0)
    expect(report.egress_today.settled_bytes).toBe(0)
  })

  it("refuse lookups with 503 once the day's compute is spent, with metadata copy", async () => {
    controlClock(T)
    await governorSql(
      'INSERT INTO days (day, cu_s) VALUES (?, ?)',
      utcDay(T),
      DAILY_CU_HOURS_CONSERVATIVE * 3600,
    )
    const calls = mockOrigin(() => apiJson('{}'))
    const response = await callWorker('/api/v1/activities/999999', visitor, deployed())
    expect(calls).toHaveLength(0)
    expect(response.status).toBe(503)
    expectSecurityHeaders(response)
    const error = await readEnvelope(response)
    expect(error.code).toBe('daily_capacity_reached')
    expect(error.details).toMatchObject({ scope: 'daily', budget: 'cu_day' })
    expect(error.message).toContain('metadata')
    expect(error.message).not.toContain('analyses')
  })

  it('fail closed when the governor is unreachable', async () => {
    const calls = mockOrigin(() => apiJson('{}'))
    const broken = {
      idFromName: () => ({}),
      get: () => ({ admitLookup: () => Promise.reject(new Error('Durable Object reset')) }),
    } as unknown as Env['GOVERNOR']
    const response = await callWorker('/api/v1/meta', visitor, { GOVERNOR: broken })
    expect(response.status).toBe(503)
    expect((await readEnvelope(response)).code).toBe('capacity_unavailable')
    expect(calls).toHaveLength(0)
  })
})
