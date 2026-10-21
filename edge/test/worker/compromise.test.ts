/**
 * What a Worker version deployed with a leaked Cloudflare token can leave
 * behind (review round 4, cf-compromise-recovery-leaves-planted-state;
 * docs/deployment.md "Rotation", item 5). Such a version runs code of its own
 * with this Worker's bindings and secrets: it can write any KV entry and any
 * governor row, and both outlive it. After the recovery (your code deployed
 * again, then every Worker secret rotated, `IP_HMAC_KEY` first) none of it
 * may count:
 *
 * - every KV entry carries an HMAC tag under a key derived from `IP_HMAC_KEY`,
 *   over its key, its metadata and its body; an entry whose tag does not
 *   verify is a miss, wherever it was written (the next `EPOCH`'s scope
 *   included);
 * - the governor's Durable Object instance is named by `IP_HMAC_KEY`, so a
 *   rotated key moves the Worker onto a fresh instance;
 * - and an instance drops state no version of this code could have written.
 */

import { runInDurableObject } from 'cloudflare:test'
import { env } from 'cloudflare:workers'
import { beforeEach, describe, expect, it } from 'vitest'
import { DAY_MS, utcDay } from '../../src/budget'
import { cacheScope, kvKey, requestHash } from '../../src/cache'
import { EGRESS_DAILY_CONSERVATIVE_BYTES } from '../../src/constants'
import { Governor as GovernorClass, type AdmitResult, type Governor } from '../../src/governor'
import {
  apiJson,
  callWorker,
  controlClock,
  governorSql,
  governorStub,
  mockOrigin,
  readEnvelope,
  resetEdgeState,
} from './helpers'

beforeEach(resetEdgeState)

const DATA_VERSION = '0325:serving-20361020T000000Z-0a1b2c3d:0123456789ab'
const RELEASE = { DATA_VERSION, ANALYTICS_VERSION: '0.2' }
const PATH = '/api/v1/analysis/estimate'
const BODY = '{"activity":{"preset":"leisure"},"years":[2024]}'
const OTHER_BODY = '{"activity":{"preset":"sleeping"},"years":[2024]}'
const RESULT = '{"estimate":{"value":1.5,"unit":"hours"}}'
const FORGED = '{"estimate":{"value":999999,"unit":"hours"},"forged":true}'
const GATE_HEADERS = {
  'x-cache': 'miss',
  'x-data-version': DATA_VERSION,
  'x-analytics-version': '0.2',
  'x-response-format': '1',
  'x-analysis-key': 'f'.repeat(64),
  'server-timing': 'db;dur=3, app;dur=1',
}
const T = Date.UTC(2036, 9, 20, 12, 0, 0)
const TODAY = utcDay(T)
const PREFIX = 'a'.repeat(32)

function post(body = BODY): [string, RequestInit] {
  return [
    PATH,
    {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'cf-connecting-ip': '198.51.100.7' },
      body,
    },
  ]
}

/** The KV key the Worker uses for `body` under `vars` (only public inputs). */
async function keyFor(body: string, vars: Partial<Env> = {}): Promise<string> {
  const scope = await cacheScope({
    EPOCH: env.EPOCH,
    API_VERSION: env.API_VERSION,
    RESPONSE_FORMAT: '1',
    ...RELEASE,
    ...vars,
  })
  return kvKey(scope!, await requestHash(PATH, '', new TextEncoder().encode(body)))
}

/** A fresh secret, so the instance it names holds nothing from another test. */
function rotatedKey(): string {
  return `rotated-ip-hmac-key-${crypto.randomUUID()}`
}

describe('KV entries a compromised version wrote', () => {
  it("are not served after an EPOCH bump, though the next EPOCH's scope is public", async () => {
    // Everything in the scope is public (/api/_edge/health, wrangler.jsonc),
    // and EPOCH is a small integer: such a version can write the next scope's
    // entries, with matching metadata and no TTL, before it is replaced.
    const health = (await (
      await callWorker('/api/_edge/health', undefined, RELEASE)
    ).json()) as Record<string, string>
    const next = await cacheScope({
      EPOCH: String(Number(env.EPOCH) + 1),
      API_VERSION: health.api_version!,
      ANALYTICS_VERSION: health.analytics_version!,
      DATA_VERSION: health.data_version!,
      RESPONSE_FORMAT: health.response_format!,
    })
    const key = kvKey(next!, await requestHash(PATH, '', new TextEncoder().encode(BODY)))
    await env.RESULT_CACHE.put(key, new TextEncoder().encode(FORGED), {
      metadata: {
        content_type: 'application/json',
        data_version: DATA_VERSION,
        analytics_version: '0.2',
        response_format: '1',
        analysis_key: null,
        stored_at: '2036-10-20T00:00:00.000Z',
        tag: 'f'.repeat(64),
      },
    })
    const calls = mockOrigin(() => apiJson(RESULT, 200, GATE_HEADERS))
    const response = await callWorker(...post(), {
      ...RELEASE,
      EPOCH: String(Number(env.EPOCH) + 1),
    })
    expect(response.headers.get('x-cache')).toBe('miss')
    expect(await response.text()).toBe(RESULT)
    expect(calls).toHaveLength(1)
  })

  it('are not served once IP_HMAC_KEY is rotated, and are replaced by the next answer', async () => {
    // The compromised version held the old key, so it could tag entries under
    // it. A rotation orphans every one of them, wherever they are.
    const before = rotatedKey()
    const after = rotatedKey()
    const calls = mockOrigin(() => apiJson(RESULT, 200, GATE_HEADERS))
    await callWorker(...post(), { ...RELEASE, IP_HMAC_KEY: before })
    const old = await callWorker(...post(), { ...RELEASE, IP_HMAC_KEY: before })
    expect(old.headers.get('x-cache')).toBe('edge')
    expect(calls).toHaveLength(1)

    const rotated = await callWorker(...post(), { ...RELEASE, IP_HMAC_KEY: after })
    expect(rotated.headers.get('x-cache')).toBe('miss')
    expect(calls).toHaveLength(2)
    const again = await callWorker(...post(), { ...RELEASE, IP_HMAC_KEY: after })
    expect(again.headers.get('x-cache')).toBe('edge')
    expect(await again.text()).toBe(RESULT)
    expect(calls).toHaveLength(2)
  })

  it("are not served when a stored entry was copied under another request's key", async () => {
    mockOrigin(() => apiJson(RESULT, 200, GATE_HEADERS))
    await callWorker(...post(), RELEASE)
    const stored = await env.RESULT_CACHE.getWithMetadata(await keyFor(BODY), 'arrayBuffer')
    expect(stored.value).not.toBeNull()
    await env.RESULT_CACHE.put(await keyFor(OTHER_BODY), stored.value!, {
      metadata: stored.metadata,
    })
    const calls = mockOrigin(() => apiJson('{"other":true}', 200, GATE_HEADERS))
    const response = await callWorker(...post(OTHER_BODY), RELEASE)
    expect(response.headers.get('x-cache')).toBe('miss')
    expect(await response.text()).toBe('{"other":true}')
    expect(calls).toHaveLength(1)
  })

  it('are not served when the body of a stored entry was replaced', async () => {
    mockOrigin(() => apiJson(RESULT, 200, GATE_HEADERS))
    await callWorker(...post(), RELEASE)
    const key = await keyFor(BODY)
    const stored = await env.RESULT_CACHE.getWithMetadata(key, 'arrayBuffer')
    await env.RESULT_CACHE.put(key, new TextEncoder().encode(FORGED), {
      metadata: stored.metadata,
    })
    const calls = mockOrigin(() => apiJson(RESULT, 200, GATE_HEADERS))
    const response = await callWorker(...post(), RELEASE)
    expect(response.headers.get('x-cache')).toBe('miss')
    expect(await response.text()).toBe(RESULT)
    expect(calls).toHaveLength(1)
  })

  it('are never read without IP_HMAC_KEY, which no tag can be checked without', async () => {
    mockOrigin(() => apiJson(RESULT, 200, GATE_HEADERS))
    await callWorker(...post(), RELEASE)
    const response = await callWorker(...post(), { ...RELEASE, IP_HMAC_KEY: '' })
    expect(response.status).toBe(503)
    expect((await readEnvelope(response)).code).toBe('capacity_unavailable')
  })
})

describe('governor state a compromised version wrote', () => {
  it('is left behind by a rotated IP_HMAC_KEY: the Worker moves to a fresh instance', async () => {
    controlClock(T)
    // Today's count at the global cap, in the instance the current key names.
    await governorSql('INSERT INTO days (day, admitted) VALUES (?, 600)', TODAY)
    const calls = mockOrigin(() => apiJson(RESULT, 200, GATE_HEADERS))
    const refused = await callWorker(...post())
    expect(refused.status).toBe(503)
    expect((await readEnvelope(refused)).code).toBe('daily_capacity_reached')

    const admitted = await callWorker(...post(), { IP_HMAC_KEY: rotatedKey() })
    expect(admitted.status).toBe(200)
    expect(calls).toHaveLength(1)
  })

  it('drops day, prefix and lease rows dated after today when an instance starts', async () => {
    // Every deploy restarts the instance (a new GovernorClass over the same
    // storage). Rows for later days can only be planted: this code writes
    // today's. Left in place, each would refuse that whole day.
    const clock = controlClock(T)
    for (let d = 1; d <= 30; d += 1) {
      await governorSql(
        'INSERT INTO days (day, admitted, heavy) VALUES (?, 600, 300)',
        utcDay(T + d * DAY_MS),
      )
      await governorSql(
        'INSERT INTO prefixes (day, prefix, count, attempts) VALUES (?, ?, 60, 300)',
        utcDay(T + d * DAY_MS),
        PREFIX,
      )
    }
    // A lease that would never expire, holding a month of egress in flight.
    await governorSql(
      `INSERT INTO leases (id, day, prefix, heavy, admitted_at, egress_bytes)
       VALUES ('planted', ?, ?, 1, ?, 1e12)`,
      utcDay(T + 365 * DAY_MS),
      PREFIX,
      T + 365 * DAY_MS,
    )
    await runInDurableObject(governorStub(), (_instance, state) => {
      expect(new GovernorClass(state, env)).toBeInstanceOf(GovernorClass)
    })
    expect(await governorSql('SELECT day FROM days WHERE day > ?', TODAY)).toEqual([])
    expect(await governorSql('SELECT day FROM prefixes WHERE day > ?', TODAY)).toEqual([])
    expect(await governorSql('SELECT id FROM leases')).toEqual([])

    clock.advance(14 * DAY_MS)
    const result = (await governorStub().admit({
      prefix: PREFIX,
      costClass: 'light',
    })) as AdmitResult
    expect(result.ok).toBe(true)
  })

  it('drops a lease whose admission time gives no date when an instance starts', async () => {
    // SQLite has no date that far back, so the day check reads NULL there:
    // such a lease must be dropped as mis-dated, not kept as "not impossible".
    // Kept, it is charged a whole provisional window as soon as anything is
    // admitted, and many of them close the day's compute.
    controlClock(T)
    await governorSql(
      `INSERT INTO leases (id, day, prefix, heavy, admitted_at, egress_bytes)
       VALUES ('undated', ?, ?, 0, ?, 1024)`,
      TODAY,
      PREFIX,
      -1e16,
    )
    await runInDurableObject(governorStub(), (_instance, state) => {
      expect(new GovernorClass(state, env)).toBeInstanceOf(GovernorClass)
    })
    expect(await governorSql('SELECT id FROM leases')).toEqual([])
  })

  it('keeps everything this code wrote when an instance starts', async () => {
    // The checks run on every start, and an idle Durable Object is evicted and
    // started again often, not only on deploys: a lease, a row or a charged
    // window they dropped by mistake would let in-flight work go uncharged.
    const clock = controlClock(T)
    const first = (await governorStub().admit({
      prefix: PREFIX,
      costClass: 'heavy',
    })) as AdmitResult
    if (!first.ok) throw new Error('denied')
    clock.advance(1000)
    await governorStub().settle(first.leaseId, { kind: 'computed', dbMs: 80 })
    clock.advance(1000)
    const analysis = (await governorStub().admit({
      prefix: PREFIX,
      costClass: 'light',
    })) as AdmitResult
    const lookup = (await governorStub().admitLookup({ prefix: PREFIX })) as AdmitResult
    if (!analysis.ok || !lookup.ok) throw new Error('denied')
    const tables = async (): Promise<unknown[]> => [
      await governorSql('SELECT * FROM days ORDER BY day'),
      await governorSql('SELECT * FROM prefixes ORDER BY day, prefix'),
      await governorSql('SELECT * FROM leases ORDER BY id'),
      await governorSql('SELECT value FROM state'),
    ]
    const before = await tables()
    expect(await governorSql('SELECT id FROM leases')).toHaveLength(2)

    clock.advance(1000)
    await runInDurableObject(governorStub(), (_instance, state) => {
      expect(new GovernorClass(state, env)).toBeInstanceOf(GovernorClass)
    })
    expect(await tables()).toEqual(before)

    // The kept charged window still covers the next settle's start, and the
    // kept lease is still settled and charged.
    const [{ cu_s: settled }] = (await governorSql<{ cu_s: number }>(
      'SELECT cu_s FROM days WHERE day = ?',
      TODAY,
    )) as [{ cu_s: number }]
    clock.advance(1000)
    await governorStub().settle(analysis.leaseId, { kind: 'computed', dbMs: 80 })
    const [{ cu_s: after }] = (await governorSql<{ cu_s: number }>(
      'SELECT cu_s FROM days WHERE day = ?',
      TODAY,
    )) as [{ cu_s: number }]
    expect(after).toBeGreaterThan(settled)
    expect(after - settled).toBeLessThan(settled)
    expect(await governorSql('SELECT id FROM leases')).toEqual([{ id: lookup.leaseId }])
  })

  it.each([
    [
      'a negative global count',
      'INSERT INTO days (day, admitted) VALUES (?, -1000000)',
      'global',
    ],
    ['a negative compute charge', 'INSERT INTO days (day, cu_s) VALUES (?, -1e12)', 'cu_day'],
    ['a text count', "INSERT INTO days (day, admitted) VALUES (?, 'none')", 'global'],
  ])("treats %s in today's row as spent, not as room", async (_label, sql, budget) => {
    // This code never writes a negative or non-numeric count; read as numbers,
    // such values would lift the day's caps for everyone.
    controlClock(T)
    await governorSql(sql, TODAY)
    const result = (await governorStub().admit({
      prefix: PREFIX,
      costClass: 'light',
    })) as AdmitResult
    expect(result).toMatchObject({ ok: false, budget })
  })

  it("treats a negative count in a network's row as spent", async () => {
    controlClock(T)
    await governorSql(
      'INSERT INTO prefixes (day, prefix, count, attempts, cu_s) VALUES (?, ?, -1000000, 0, -1e12)',
      TODAY,
      PREFIX,
    )
    const result = (await governorStub().admit({
      prefix: PREFIX,
      costClass: 'light',
    })) as AdmitResult
    expect(result).toMatchObject({ ok: false, budget: 'prefix' })
  })

  it('ignores a charged compute window that ends later than any settle could reach', async () => {
    // A window to the end of time covered every later analysis: the day's
    // ceiling and each network's share never charged anything again.
    const clock = controlClock(T)
    await governorSql(
      'INSERT INTO state (id, value) VALUES (1, ?)',
      JSON.stringify({ charged: [[0, 8.64e15]] }),
    )
    const result = (await governorStub().admit({
      prefix: PREFIX,
      costClass: 'light',
    })) as AdmitResult
    if (!result.ok) throw new Error('denied')
    clock.advance(1000)
    await governorStub().settle(result.leaseId, { kind: 'computed', dbMs: 80 })
    const [day] = await governorSql<{ cu_s: number }>(
      'SELECT cu_s FROM days WHERE day = ?',
      TODAY,
    )
    expect(day!.cu_s).toBeGreaterThan(600)
  })

  it('ignores Neon ground truth while no Neon key is configured', async () => {
    // Without a key no poll can have written it; trusted, it replaced the
    // daily egress check with a monthly one fed by its own (planted) figures.
    controlClock(T)
    await governorSql(
      'INSERT INTO days (day, egress_bytes) VALUES (?, ?)',
      TODAY,
      EGRESS_DAILY_CONSERVATIVE_BYTES,
    )
    await governorSql(
      'INSERT INTO state (id, value) VALUES (1, ?)',
      JSON.stringify({ neon: groundTruth() }),
    )
    const result = (await governorStub().admit({
      prefix: PREFIX,
      costClass: 'heavy',
    })) as AdmitResult
    expect(result).toMatchObject({ ok: false, budget: 'egress_day' })
    expect((await governorStub().budget()).month.period_end).toBeNull()
  })

  it.each([
    ['a consumption period ending a year after the poll', { periodEnd: T + 365 * DAY_MS }],
    ['a poll in the future', { polledAt: T + DAY_MS }],
    ['a bridge base above the bridge counter', { cuBaseS: 1e9, cuAtPollS: 1e9 }],
    ['negative usage', { computeTimeSeconds: -1e9 }],
  ])(
    'drops Neon ground truth with %s, which no poll could have produced',
    async (_label, fields) => {
      controlClock(T)
      await runInDurableObject(governorStub(), (instance: Governor) => {
        ;(instance as unknown as { env: Env }).env = {
          ...env,
          NEON_API_KEY: 'neon-test-key',
          NEON_PROJECT_ID: 'proud-sun-123456',
        }
      })
      try {
        await governorSql(
          'INSERT INTO state (id, value) VALUES (1, ?)',
          JSON.stringify({ neon: groundTruth(fields) }),
        )
        const report = await governorStub().budget()
        expect(report.neon_configured).toBe(true)
        expect(report.mode).toBe('conservative')
        expect(report.month.period_end).toBeNull()
      } finally {
        await runInDurableObject(governorStub(), (instance: Governor) => {
          ;(instance as unknown as { env: Env }).env = env
        })
      }
    },
  )
})

/** Ground truth as a poll at `T` would leave it, with `fields` changed. */
function groundTruth(fields: Record<string, number> = {}): Record<string, number> {
  return {
    polledAt: T,
    computeTimeSeconds: 0,
    dataTransferBytes: 0,
    periodStart: Date.UTC(2036, 9, 1),
    periodEnd: Date.UTC(2036, 10, 1),
    cuBaseS: 0,
    egressBase: 0,
    cuAtPollS: 0,
    egressAtPoll: 0,
    ...fields,
  }
}
