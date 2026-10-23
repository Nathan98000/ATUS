/**
 * Governor admission as the Worker applies it (ADR-011 step 3.5): which
 * requests are admitted, the client prefix they are charged to, the 429/503
 * envelopes of a denial (no origin call), failing closed when the governor
 * is unreachable, and how each origin outcome is settled.
 */

import {
  createExecutionContext,
  runInDurableObject,
  waitOnExecutionContext,
} from 'cloudflare:test'
import { env } from 'cloudflare:workers'
import { beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { prefixHmac } from '../../src/admission'
import { nextUtcMidnight, utcDay } from '../../src/budget'
import type { Governor } from '../../src/governor'
import worker from '../../src/index'
import {
  BASE,
  EDGE_SECRET,
  apiJson,
  callWorker,
  controlClock,
  expectSecurityHeaders,
  forgedAddressHeaders,
  governorSql,
  governorStub,
  mockOrigin,
  readEnvelope,
  resetEdgeState,
} from './helpers'

beforeEach(resetEdgeState)

const T = Date.UTC(2036, 9, 20, 12, 0, 0)
const MIDNIGHT = nextUtcMidnight(T)
const IP_KEY = 'test-ip-hmac-key'
const PATH = '/api/v1/analysis/estimate'
const COMPUTED = { 'x-cache': 'miss', 'server-timing': 'db;dur=4, app;dur=1' }

function post(
  ip: string | null = '203.0.113.7',
  body = '{"years":[2024]}',
  path = PATH,
): [string, RequestInit] {
  const headers: Record<string, string> = { 'content-type': 'application/json' }
  if (ip !== null) headers['cf-connecting-ip'] = ip
  return [path, { method: 'POST', headers, body }]
}

async function report(): Promise<
  Awaited<ReturnType<ReturnType<typeof governorStub>['budget']>>
> {
  return governorStub().budget()
}

function brokenGovernor(admit: () => Promise<unknown>, settle?: () => Promise<unknown>) {
  return {
    idFromName: () => ({}),
    get: () => ({ admit, settle: settle ?? (() => Promise.resolve()) }),
  } as unknown as Env['GOVERNOR']
}

describe('admission', () => {
  it('admits an uncached analysis, then settles it once the response is sent', async () => {
    controlClock(T)
    const calls = mockOrigin(() => apiJson('{"ok":true}', 200, COMPUTED))
    const response = await callWorker(...post())
    expect(response.status).toBe(200)
    expect(calls).toHaveLength(1)
    const budget = await report()
    expect(budget.analyses).toMatchObject({ admitted: 1, in_flight: 0, refunded: 0 })
    expect(budget.compute_today.settled_cu_hours).toBeGreaterThan(0)
  })

  it('charges the /24 under the daily HMAC, never the address', async () => {
    controlClock(T)
    mockOrigin(() => apiJson('{"ok":true}', 200, COMPUTED))
    await callWorker(...post('203.0.113.7'))
    await callWorker(...post('203.0.113.200'))
    await callWorker(...post('198.51.100.1'))
    await callWorker(...post('2001:db8:abcd:1::5'))
    await callWorker(...post('2001:db8:abcd:ffff::9'))
    const rows = await governorSql<{ prefix: string; count: number }>(
      'SELECT prefix, count FROM prefixes ORDER BY count DESC, prefix',
    )
    const expected = new Map([
      [await prefixHmac(IP_KEY, T, '203.0.113.0/24'), 2],
      [await prefixHmac(IP_KEY, T, '2001:0db8:abcd::/48'), 2],
      [await prefixHmac(IP_KEY, T, '198.51.100.0/24'), 1],
    ])
    expect(new Map(rows.map((row) => [row.prefix, row.count]))).toEqual(expected)
    const stored = JSON.stringify(await governorSql('SELECT * FROM prefixes'))
    for (const fragment of ['203.0.113', '198.51', '2001:db8', 'db8']) {
      expect(stored).not.toContain(fragment)
    }
  })

  it('charges requests without a usable client address to one shared bucket', async () => {
    controlClock(T)
    mockOrigin(() => apiJson('{"ok":true}', 200, COMPUTED))
    await callWorker(...post(null))
    await callWorker(...post('not-an-ip'))
    const rows = await governorSql<{ prefix: string; count: number }>(
      'SELECT prefix, count FROM prefixes',
    )
    expect(rows).toEqual([{ prefix: await prefixHmac(IP_KEY, T, 'unknown'), count: 2 }])
  })

  it('classifies a 23-year trend as heavy and a one-year estimate as light', async () => {
    controlClock(T)
    mockOrigin(() => apiJson('{"ok":true}', 200, COMPUTED))
    const years = JSON.stringify({ years: Array.from({ length: 23 }, (_, i) => 2003 + i) })
    await callWorker(...post('203.0.113.7', years, '/api/v1/analysis/trend'))
    await callWorker(...post())
    expect((await report()).analyses).toMatchObject({ admitted: 2, heavy: 1 })
  })

  it('counts metadata GETs sent to the origin as lookups, never as analyses', async () => {
    mockOrigin(() => apiJson('{"api_version":"v1"}'))
    await callWorker('/api/v1/meta')
    await callWorker('/api/v1/activities')
    await callWorker('/api/health')
    const budget = await report()
    expect(budget.analyses.admitted).toBe(0)
    expect(budget.metadata.origin_calls).toBe(2)
  })

  it('logs the governor and cache results without the client address or prefix hash', async () => {
    controlClock(T)
    mockOrigin(() => apiJson('{"ok":true}', 200, COMPUTED))
    const lines: string[] = []
    vi.spyOn(console, 'log').mockImplementation((line: unknown) => {
      lines.push(String(line))
    })
    await callWorker(...post('203.0.113.7'))
    const record = JSON.parse(lines.find((line) => line.includes('"event":"api"'))!) as Record<
      string,
      unknown
    >
    expect(record).toMatchObject({ governor: 'admitted', cache: 'off', status: 200 })
    const text = lines.join('\n')
    expect(text).not.toContain('203.0.113')
    expect(text).not.toContain(await prefixHmac(IP_KEY, T, '203.0.113.0/24'))
    expect(text).not.toContain(EDGE_SECRET)
  })
})

describe('denials (no origin call)', () => {
  it('answers 429 rate_limited once the network has used its 60 for the day', async () => {
    controlClock(T)
    const prefix = await prefixHmac(IP_KEY, T, '203.0.113.0/24')
    await governorSql(
      'INSERT INTO prefixes (day, prefix, count) VALUES (?, ?, 60)',
      utcDay(T),
      prefix,
    )
    const calls = mockOrigin(() => apiJson('{}'))
    const response = await callWorker(...post('203.0.113.99'))
    expect(calls).toHaveLength(0)
    expect(response.status).toBe(429)
    const seconds = (MIDNIGHT - T) / 1000
    expect(response.headers.get('retry-after')).toBe(String(seconds))
    expectSecurityHeaders(response)
    const error = await readEnvelope(response)
    expect(error.code).toBe('rate_limited')
    expect(error.details).toEqual({
      scope: 'client',
      budget: 'prefix',
      resumes_at: new Date(MIDNIGHT).toISOString(),
      retry_after_seconds: seconds,
    })
    // Another network is still admitted.
    mockOrigin(() => apiJson('{"ok":true}', 200, COMPUTED))
    expect((await callWorker(...post('198.51.100.1'))).status).toBe(200)
  })

  it('charges the network of CF-Connecting-IP whatever forwarding headers the client adds', async () => {
    // Brief §17 (proxy-chain client IP, trusted versus forged headers) and
    // security.md abuse case 5. Review r3: no test sent a forged header to the
    // governor, and keying it on X-Forwarded-For passed every Worker test,
    // which would let one client claim a new network on every request.
    controlClock(T)
    const full = await prefixHmac(IP_KEY, T, '203.0.113.0/24')
    await governorSql(
      'INSERT INTO prefixes (day, prefix, count) VALUES (?, ?, 60)',
      utcDay(T),
      full,
    )
    const calls = mockOrigin(() => apiJson('{"ok":true}', 200, COMPUTED))
    const forged = forgedAddressHeaders('198.51.100.1')
    const withHeaders = (ip: string, extra: Record<string, string>): [string, RequestInit] => {
      const [path, init] = post(ip)
      return [
        path,
        { ...init, headers: { ...(init.headers as Record<string, string>), ...extra } },
      ]
    }
    for (const [name, value] of [...forged, ['all of them', '']] as const) {
      const extra = name === 'all of them' ? Object.fromEntries(forged) : { [name]: value }
      const response = await callWorker(...withHeaders('203.0.113.9', extra))
      expect(response.status, name).toBe(429)
      expect((await readEnvelope(response)).details, name).toMatchObject({
        scope: 'client',
        budget: 'prefix',
      })
    }
    expect(calls).toHaveLength(0)
    // The other way round: naming the full network does not charge it either.
    for (const [name, value] of forgedAddressHeaders('203.0.113.9')) {
      const response = await callWorker(...withHeaders('192.0.2.77', { [name]: value }))
      expect(response.status, name).toBe(200)
    }
    const rows = await governorSql<{ prefix: string; count: number }>(
      'SELECT prefix, count FROM prefixes',
    )
    expect(new Map(rows.map((row) => [row.prefix, row.count]))).toEqual(
      new Map([
        [full, 60],
        [await prefixHmac(IP_KEY, T, '192.0.2.0/24'), forged.length],
      ]),
    )
  })

  it('bounds admissions the API sheds (refunded), so one network cannot spend the governor', async () => {
    controlClock(T)
    // Past its own per-client rate, the API sheds every analysis with 429
    // rate_limited; each is refunded, so the 60-analysis budget never trips.
    const shed = JSON.stringify({
      error: { code: 'rate_limited', message: 'x', details: null },
    })
    const calls = mockOrigin(() => apiJson(shed, 429, { 'retry-after': '10' }))
    for (let i = 0; i < 300; i += 1) {
      const response = await callWorker(...post('203.0.113.7', `{"years":[2024],"n":${i}}`))
      expect(response.status).toBe(429)
    }
    expect(calls).toHaveLength(300)
    const budget = await report()
    expect(budget.analyses).toMatchObject({ admitted: 0, refunded: 300 })
    expect(budget.prefixes).toMatchObject({ max_admitted: 0, max_attempts: 300 })
    // The 301st is refused at the edge, until midnight, without an origin call.
    const refused = await callWorker(...post('203.0.113.8', '{"years":[2024],"n":300}'))
    expect(calls).toHaveLength(300)
    expect(refused.status).toBe(429)
    expect(refused.headers.get('retry-after')).toBe(String((MIDNIGHT - T) / 1000))
    expect((await readEnvelope(refused)).details).toMatchObject({
      scope: 'client',
      budget: 'prefix',
    })
    // Another network is unaffected.
    mockOrigin(() => apiJson('{"ok":true}', 200, COMPUTED))
    expect((await callWorker(...post('198.51.100.1'))).status).toBe(200)
  })

  it('answers 503 daily_capacity_reached when the global daily budget is spent', async () => {
    controlClock(T)
    await governorSql('INSERT INTO days (day, admitted) VALUES (?, 600)', utcDay(T))
    const calls = mockOrigin(() => apiJson('{}'))
    const response = await callWorker(...post())
    expect(calls).toHaveLength(0)
    expect(response.status).toBe(503)
    expect(response.headers.get('retry-after')).toBe(String((MIDNIGHT - T) / 1000))
    const error = await readEnvelope(response)
    expect(error.code).toBe('daily_capacity_reached')
    expect(error.details).toMatchObject({ scope: 'global', budget: 'global' })
  })

  it('answers 503 capacity_paused until the Neon period ends past the monthly hard stop', async () => {
    controlClock(T)
    // Ground truth counts only while a Neon key is configured (review round 4:
    // without one, no poll can have written it; compromise.test.ts).
    await runInDurableObject(governorStub(), (instance: Governor) => {
      ;(instance as unknown as { env: Env }).env = {
        ...env,
        NEON_API_KEY: 'neon-test-key',
        NEON_PROJECT_ID: 'proud-sun-123456',
      }
    })
    onTestFinished(() =>
      runInDurableObject(governorStub(), (instance: Governor) => {
        ;(instance as unknown as { env: Env }).env = env
      }),
    )
    const periodEnd = Date.UTC(2036, 10, 1)
    const neon = {
      polledAt: T,
      computeTimeSeconds: 85 * 3600,
      dataTransferBytes: 0,
      periodStart: Date.UTC(2036, 9, 1),
      periodEnd,
      cuBaseS: 0,
      egressBase: 0,
      cuAtPollS: 0,
      egressAtPoll: 0,
    }
    await governorSql('INSERT INTO state (id, value) VALUES (1, ?)', JSON.stringify({ neon }))
    const calls = mockOrigin(() => apiJson('{}'))
    const response = await callWorker(...post())
    expect(calls).toHaveLength(0)
    expect(response.status).toBe(503)
    expect(response.headers.get('retry-after')).toBe(String((periodEnd - T) / 1000))
    const error = await readEnvelope(response)
    expect(error.code).toBe('capacity_paused')
    expect(error.details).toMatchObject({
      scope: 'monthly',
      budget: 'cu_month',
      resumes_at: new Date(periodEnd).toISOString(),
    })
  })
})

describe('failing closed', () => {
  it('answers 503 capacity_unavailable when the governor errors, and nothing else is affected', async () => {
    const calls = mockOrigin(() => apiJson('{"api_version":"v1"}'))
    const governor = brokenGovernor(() => Promise.reject(new Error('Durable Object reset')))
    const response = await callWorker(...post(), { GOVERNOR: governor })
    expect(response.status).toBe(503)
    expect(response.headers.get('retry-after')).toBe('60')
    expectSecurityHeaders(response)
    const error = await readEnvelope(response)
    expect(error.code).toBe('capacity_unavailable')
    expect(error.details).toEqual({ retry_after_seconds: 60 })
    expect(calls).toHaveLength(0)
    // Health still goes through (snapshot metadata too: metadata.test.ts).
    expect((await callWorker('/api/health', undefined, { GOVERNOR: governor })).status).toBe(
      200,
    )
    expect(
      (await callWorker('/api/_edge/health', undefined, { GOVERNOR: governor })).status,
    ).toBe(200)
  })

  it(
    'fails closed when the governor does not answer within 5 s',
    { timeout: 15_000 },
    async () => {
      const calls = mockOrigin(() => apiJson('{}'))
      const governor = brokenGovernor(() => new Promise(() => {}))
      const response = await callWorker(...post(), { GOVERNOR: governor })
      expect(response.status).toBe(503)
      expect((await readEnvelope(response)).code).toBe('capacity_unavailable')
      expect(calls).toHaveLength(0)
    },
  )

  it('fails closed when IP_HMAC_KEY is not configured', async () => {
    const calls = mockOrigin(() => apiJson('{}'))
    const response = await callWorker(...post(), { IP_HMAC_KEY: '' })
    expect(response.status).toBe(503)
    expect((await readEnvelope(response)).code).toBe('capacity_unavailable')
    expect(calls).toHaveLength(0)
    expect((await report()).analyses.admitted).toBe(0)
  })

  it('treats a malformed governor answer as unavailable', async () => {
    mockOrigin(() => apiJson('{}'))
    const governor = brokenGovernor(() => Promise.resolve({ ok: 'yes' }))
    const response = await callWorker(...post(), { GOVERNOR: governor })
    expect((await readEnvelope(response)).code).toBe('capacity_unavailable')
  })

  it('queues the settle before the origin answers, so a client that leaves meanwhile still settles', async () => {
    // Cloudflare may cancel a request's work once its client disconnects;
    // `waitUntil` work registered before that gets up to 30 s more.
    controlClock(T)
    const origin: { answer?: (response: Response) => void } = {}
    mockOrigin(
      () =>
        new Promise<Response>((resolve) => {
          origin.answer = resolve
        }),
    )
    const ctx = createExecutionContext()
    const [path, init] = post()
    const handled = worker.fetch(
      new Request(BASE + path, init) as Request<unknown, IncomingRequestCfProperties>,
      env,
      ctx,
    )
    for (let i = 0; i < 100 && origin.answer === undefined; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 5))
    }
    expect(await governorSql('SELECT id FROM leases')).toHaveLength(1)
    // waitOnExecutionContext takes the promises queued so far, so this drain
    // owns the settle: await it below rather than draining again, which would
    // return before the settle finished on a slow runner.
    const drained = waitOnExecutionContext(ctx)
    const queued = await Promise.race([
      drained.then(() => 'nothing queued'),
      new Promise((resolve) => setTimeout(() => resolve('settle queued'), 200)),
    ])
    expect(queued).toBe('settle queued')
    origin.answer!(apiJson('{"ok":true}', 200, COMPUTED))
    expect((await handled).status).toBe(200)
    await drained
    await waitOnExecutionContext(ctx)
    expect(await governorSql('SELECT id FROM leases')).toEqual([])
    expect((await report()).analyses).toMatchObject({ admitted: 1, in_flight: 0 })
  })

  it('never fails a request because settling failed', async () => {
    mockOrigin(() => apiJson('{"ok":true}', 200, COMPUTED))
    const governor = brokenGovernor(
      () => Promise.resolve({ ok: true, leaseId: 'lease-1' }),
      () => Promise.reject(new Error('Durable Object reset')),
    )
    const response = await callWorker(...post(), { GOVERNOR: governor })
    expect(response.status).toBe(200)
    expect(await response.text()).toBe('{"ok":true}')
  })
})

describe('settling each origin outcome', () => {
  async function settledAfter(origin: () => Response): Promise<{
    status: number
    admitted: number
    refunded: number
    prefix: number
  }> {
    controlClock(T)
    mockOrigin(origin)
    const response = await callWorker(...post())
    const budget = await report()
    const rows = await governorSql<{ count: number }>('SELECT count FROM prefixes')
    return {
      status: response.status,
      admitted: budget.analyses.admitted,
      refunded: budget.analyses.refunded,
      prefix: rows[0]?.count ?? 0,
    }
  }
  const envelope = (code: string): string =>
    JSON.stringify({ error: { code, message: 'x', details: null } })

  it('refunds everything when Render is waking (the API never answered)', async () => {
    const outcome = await settledAfter(
      () =>
        new Response(null, {
          status: 503,
          headers: { 'x-render-routing': 'dynamic-hibernate-error-503' },
        }),
    )
    expect(outcome).toEqual({ status: 503, admitted: 0, refunded: 1, prefix: 0 })
  })

  it.each([520, 521, 522, 523, 525, 526, 530])(
    'refunds everything after a Cloudflare %i (the API never took the request)',
    async (status) => {
      const outcome = await settledAfter(
        () => new Response('<html>', { status, headers: { 'content-type': 'text/html' } }),
      )
      expect(outcome).toEqual({ status: 502, admitted: 0, refunded: 1, prefix: 0 })
      expect((await report()).compute_today.settled_cu_hours).toBe(0)
    },
  )

  it('keeps the charge after a Cloudflare 524 (the API took the request and kept computing)', async () => {
    const outcome = await settledAfter(
      () => new Response('<html>', { status: 524, headers: { 'content-type': 'text/html' } }),
    )
    expect(outcome).toEqual({ status: 502, admitted: 1, refunded: 0, prefix: 1 })
    // No Server-Timing: charged from admission, plus the 300 s tail, at 2 CU.
    expect((await report()).compute_today.settled_cu_hours).toBeCloseTo(600 / 3600, 4)
  })

  it('does not call the governor while the circuit breaker skips the origin', async () => {
    controlClock(T)
    const calls = mockOrigin(() => {
      throw new TypeError('Network connection lost.')
    })
    // Two connection failures open the breaker (/api/health is never governed).
    expect((await callWorker('/api/health')).status).toBe(502)
    const response = await callWorker(...post())
    expect(calls).toHaveLength(2)
    expect(response.status).toBe(503)
    expect(response.headers.get('retry-after')).toBe('30')
    expect((await readEnvelope(response)).code).toBe('upstream_unavailable')
    expect((await report()).analyses).toMatchObject({ admitted: 0, refunded: 0, in_flight: 0 })
    expect(await governorSql('SELECT prefix FROM prefixes')).toEqual([])
  })

  it('refunds everything after a connection failure', async () => {
    const outcome = await settledAfter(() => {
      throw new TypeError('Network connection lost.')
    })
    expect(outcome).toEqual({ status: 502, admitted: 0, refunded: 1, prefix: 0 })
  })

  it('refunds everything when the API sheds the request (overloaded)', async () => {
    const outcome = await settledAfter(() => apiJson(envelope('overloaded'), 503))
    expect(outcome).toEqual({ status: 503, admitted: 0, refunded: 1, prefix: 0 })
  })

  it('refunds the counts and charges no compute when Neon refused the connection (database_paused)', async () => {
    // The API times the failed connection as DB time; Neon ran nothing.
    const outcome = await settledAfter(() =>
      apiJson(envelope('database_paused'), 503, {
        'server-timing': 'db;dur=12.0, app;dur=13.0',
      }),
    )
    expect(outcome).toEqual({ status: 503, admitted: 0, refunded: 1, prefix: 0 })
    expect((await report()).compute_today.settled_cu_hours).toBe(0)
  })

  it('refunds the counts and charges no compute when the API got no database connection (database_unavailable)', async () => {
    // Neon unreachable, wrong credentials, a TLS failure: the API reports
    // db;dur=0 because no connection was obtained, and Neon ran nothing.
    const outcome = await settledAfter(() =>
      apiJson(envelope('database_unavailable'), 503, {
        'server-timing': 'db;dur=0, app;dur=1004.2',
      }),
    )
    expect(outcome).toEqual({ status: 503, admitted: 0, refunded: 1, prefix: 0 })
    expect((await report()).compute_today.settled_cu_hours).toBe(0)
  })

  it('refunds the counts but charges compute when the connection broke after the checkout', async () => {
    const outcome = await settledAfter(() =>
      apiJson(envelope('database_unavailable'), 503, {
        'server-timing': 'db;dur=40.0, app;dur=2.0',
      }),
    )
    expect(outcome).toEqual({ status: 503, admitted: 0, refunded: 1, prefix: 0 })
    expect((await report()).compute_today.settled_cu_hours).toBeGreaterThan(0)
  })

  it('lets a run of failed database connections lock out neither a network nor the day', async () => {
    // Review r3 (db-unavailable-settled-as-computed): each 503
    // database_unavailable was settled as computed. In conservative mode one
    // network trying every 6 minutes (an attempt and the SPA's retry) got 429
    // rate_limited (prefix) from its fourth attempt, and about 16 networks
    // closed the day's compute (cu_day) for everyone until 00:00 UTC, after
    // the database was fixed.
    const clock = controlClock(T)
    const calls = mockOrigin(() =>
      apiJson(envelope('database_unavailable'), 503, {
        'retry-after': '5',
        'server-timing': 'db;dur=0, app;dur=1004.2',
      }),
    )
    // 35 rounds 6 minutes apart: 70 admissions for 203.0.113.7, past its 60.
    for (let round = 0; round < 35; round += 1) {
      for (const ip of ['203.0.113.7', `198.51.100.${round + 1}`]) {
        for (let attempt = 0; attempt < 2; attempt += 1) {
          const response = await callWorker(...post(ip))
          expect(response.status).toBe(503)
          expect((await readEnvelope(response)).code).toBe('database_unavailable')
          clock.advance(5_000)
        }
      }
      clock.advance(6 * 60_000)
    }
    expect(calls).toHaveLength(140)
    const during = await report()
    expect(during.analyses).toMatchObject({ admitted: 0, refunded: 140, in_flight: 0 })
    expect(during.compute_today.settled_cu_hours).toBe(0)
    // The operator fixes the connection: the same network and a new one are admitted.
    mockOrigin(() => apiJson('{"ok":true}', 200, COMPUTED))
    expect((await callWorker(...post('203.0.113.7'))).status).toBe(200)
    expect((await callWorker(...post('192.0.2.50'))).status).toBe(200)
  })

  it.each(['data_version_mismatch', 'data_not_loaded', 'schema_not_initialized'])(
    'refunds the counts of a %s refusal (no analysis ran)',
    async (code) => {
      const outcome = await settledAfter(() => apiJson(envelope(code), 503))
      expect(outcome).toEqual({ status: 503, admitted: 0, refunded: 1, prefix: 0 })
    },
  )

  it('refunds the day but keeps the network charge for a validation error', async () => {
    const outcome = await settledAfter(() => apiJson(envelope('validation_error'), 422))
    expect(outcome).toEqual({ status: 422, admitted: 0, refunded: 1, prefix: 1 })
  })

  it('refunds the day for a result the API served from its cache', async () => {
    const outcome = await settledAfter(() => apiJson('{"ok":true}', 200, { 'x-cache': 'hit' }))
    expect(outcome).toEqual({ status: 200, admitted: 0, refunded: 1, prefix: 1 })
  })

  it('keeps the charge for a computed result and for an analysis timeout', async () => {
    expect(await settledAfter(() => apiJson('{"ok":true}', 200, COMPUTED))).toEqual({
      status: 200,
      admitted: 1,
      refunded: 0,
      prefix: 1,
    })
    await resetEdgeState()
    expect(await settledAfter(() => apiJson(envelope('analysis_timeout'), 503))).toEqual({
      status: 503,
      admitted: 1,
      refunded: 0,
      prefix: 1,
    })
  })
})
