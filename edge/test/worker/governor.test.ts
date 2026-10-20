/**
 * The quota governor Durable Object (src/governor.ts), driven through its
 * RPC methods with a controlled clock: admission budgets, the provisional
 * and settled compute charges, refunds, ground truth from the Neon API,
 * the midnight purge and the ops report.
 */

import { runDurableObjectAlarm, runInDurableObject } from 'cloudflare:test'
import { env } from 'cloudflare:workers'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { DAY_MS, nextUtcMidnight, pacingResumesAt, utcDay } from '../../src/budget'
import { DAILY_CU_HOURS_CONSERVATIVE } from '../../src/constants'
import type { AdmitResult, Denied, Governor, SettleKind } from '../../src/governor'
import { controlClock, governorSql, governorStub, resetEdgeState } from './helpers'

// In the future, so an alarm the governor schedules never fires by itself
// during a test: only runDurableObjectAlarm runs it.
const T = Date.UTC(2036, 9, 20, 12, 0, 0)
const TODAY = utcDay(T)
const YESTERDAY = utcDay(T - DAY_MS)
const MIDNIGHT = nextUtcMidnight(T)
const S = 1000
const MIN = 60 * S
const PREFIX = 'a'.repeat(32)
const OTHER = 'b'.repeat(32)
const NEON_KEY = 'neon-test-key-do-not-log'
const NEON_PROJECT = 'proud-sun-123456'
const PERIOD_START = Date.UTC(2036, 9, 1)
const PERIOD_END = Date.UTC(2036, 10, 1)

beforeEach(async () => {
  await resetEdgeState()
  await setNeon(null)
})

const governor = governorStub

async function admit(
  prefix = PREFIX,
  costClass: 'light' | 'heavy' = 'light',
): Promise<AdmitResult> {
  return (await governor().admit({ prefix, costClass })) as AdmitResult
}

async function admitted(
  prefix = PREFIX,
  costClass: 'light' | 'heavy' = 'light',
): Promise<string> {
  const result = await admit(prefix, costClass)
  if (!result.ok) throw new Error(`denied: ${result.budget}`)
  return result.leaseId
}

async function denied(
  prefix = PREFIX,
  costClass: 'light' | 'heavy' = 'light',
): Promise<Denied> {
  const result = await admit(prefix, costClass)
  if (result.ok) throw new Error('admitted')
  return result
}

async function settle(
  leaseId: string,
  kind: SettleKind,
  dbMs: number | null = null,
): Promise<void> {
  await governor().settle(leaseId, { kind, dbMs })
}

type DayTotals = {
  admitted: number
  heavy: number
  cu_s: number
  egress_bytes: number
  refunded: number
}

async function day(dayName = TODAY): Promise<DayTotals> {
  const rows = await governorSql<DayTotals>(
    'SELECT admitted, heavy, cu_s, egress_bytes, refunded FROM days WHERE day = ?',
    dayName,
  )
  return rows[0] ?? { admitted: 0, heavy: 0, cu_s: 0, egress_bytes: 0, refunded: 0 }
}

async function prefixCount(prefix = PREFIX, dayName = TODAY): Promise<number> {
  const rows = await governorSql<{ count: number }>(
    'SELECT count FROM prefixes WHERE day = ? AND prefix = ?',
    dayName,
    prefix,
  )
  return rows[0]?.count ?? 0
}

async function storedState(): Promise<string> {
  const rows = await governorSql<{ value: string }>('SELECT value FROM state WHERE id = 1')
  return rows[0]?.value ?? ''
}

async function alarmAt(): Promise<number | null> {
  return runInDurableObject(governor(), (_instance, state) => state.storage.getAlarm())
}

/** Point the governor's Neon settings at a test project (or clear them). */
async function setNeon(config: { key: string; project: string } | null): Promise<void> {
  await runInDurableObject(governor(), (instance: Governor) => {
    const target = instance as unknown as { env: Env }
    target.env =
      config === null
        ? env
        : { ...env, NEON_API_KEY: config.key, NEON_PROJECT_ID: config.project }
  })
}

interface NeonProject {
  compute_time_seconds: number
  data_transfer_bytes: number
  consumption_period_start: string
  consumption_period_end: string
}

/** Mock the Neon API; returns the recorded calls. */
function mockNeon(
  project: Partial<NeonProject> | (() => Response),
): { url: string; headers: Headers }[] {
  const calls: { url: string; headers: Headers }[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    calls.push({ url: String(input), headers: new Headers(init?.headers) })
    if (typeof project === 'function') return project()
    return Response.json({
      project: {
        id: NEON_PROJECT,
        compute_time_seconds: 0,
        data_transfer_bytes: 0,
        consumption_period_start: new Date(PERIOD_START).toISOString(),
        consumption_period_end: new Date(PERIOD_END).toISOString(),
        ...project,
      },
    })
  })
  return calls
}

describe('daily counts', () => {
  it('admits with a lease and charges the day and the prefix', async () => {
    controlClock(T)
    const leaseId = await admitted()
    expect(leaseId).toMatch(/^[0-9a-f-]{36}$/)
    expect(await day()).toMatchObject({ admitted: 1, heavy: 0 })
    expect(await prefixCount()).toBe(1)
    const leases = await governorSql('SELECT id, day, prefix, heavy FROM leases')
    expect(leases).toEqual([{ id: leaseId, day: TODAY, prefix: PREFIX, heavy: 0 }])
  })

  it('counts heavy analyses, and treats an unknown cost class as heavy', async () => {
    controlClock(T)
    await admitted(PREFIX, 'heavy')
    await governor().admit({ prefix: PREFIX, costClass: 'bogus' as 'light' })
    expect(await day()).toMatchObject({ admitted: 2, heavy: 2 })
  })

  it('refuses the 61st analysis from one prefix until midnight, other prefixes unaffected', async () => {
    controlClock(T)
    for (let i = 0; i < 60; i += 1) await admitted()
    const denial = await denied()
    expect(denial).toEqual({
      ok: false,
      code: 'rate_limited',
      scope: 'client',
      budget: 'prefix',
      retryAfterSeconds: (MIDNIGHT - T) / 1000,
      resumesAt: new Date(MIDNIGHT).toISOString(),
    })
    await admitted(OTHER)
    expect(await day()).toMatchObject({ admitted: 61 })
  })

  it('refuses the 601st analysis of the UTC day for everyone (scope global)', async () => {
    controlClock(T)
    await governorSql('INSERT INTO days (day, admitted) VALUES (?, 600)', TODAY)
    const denial = await denied(OTHER)
    expect(denial).toMatchObject({
      code: 'daily_capacity_reached',
      scope: 'global',
      budget: 'global',
      resumesAt: new Date(MIDNIGHT).toISOString(),
    })
  })

  it('refuses heavy analyses past 300 a day but still admits light ones', async () => {
    controlClock(T)
    await governorSql('INSERT INTO days (day, admitted, heavy) VALUES (?, 300, 300)', TODAY)
    expect(await denied(PREFIX, 'heavy')).toMatchObject({
      code: 'daily_capacity_reached',
      scope: 'daily',
      budget: 'heavy',
    })
    await admitted(PREFIX, 'light')
  })

  it("starts every UTC day fresh and deletes the previous days' prefix rows", async () => {
    controlClock(T)
    await governorSql('INSERT INTO days (day, admitted, heavy) VALUES (?, 600, 300)', YESTERDAY)
    await governorSql(
      'INSERT INTO prefixes (day, prefix, count) VALUES (?, ?, 60)',
      YESTERDAY,
      PREFIX,
    )
    await admitted(PREFIX, 'heavy')
    expect(await prefixCount(PREFIX, YESTERDAY)).toBe(0)
    expect(await prefixCount(PREFIX, TODAY)).toBe(1)
    // Day totals are kept (for the ops report's 31-day sum).
    expect(await day(YESTERDAY)).toMatchObject({ admitted: 600 })
  })

  it('writes nothing when it denies', async () => {
    controlClock(T)
    await governorSql(
      'INSERT INTO prefixes (day, prefix, count) VALUES (?, ?, 60)',
      TODAY,
      PREFIX,
    )
    await governorSql('INSERT INTO days (day, admitted) VALUES (?, 600)', TODAY)
    const statements: string[] = []
    await runInDurableObject(governor(), (instance: Governor) => {
      const target = instance as unknown as { sql: SqlStorage }
      const real = target.sql
      target.sql = {
        exec: (query: string, ...bindings: SqlStorageValue[]) => {
          statements.push(query)
          return real.exec(query, ...bindings)
        },
      } as unknown as SqlStorage
    })
    try {
      await denied(PREFIX)
      await denied(OTHER)
    } finally {
      await runInDurableObject(governor(), (instance: Governor, state) => {
        ;(instance as unknown as { sql: SqlStorage }).sql = state.storage.sql
      })
    }
    expect(statements.length).toBeGreaterThan(0)
    for (const statement of statements) expect(statement.trim()).toMatch(/^SELECT/i)
  })
})

describe('compute charge (conservative mode)', () => {
  it('charges a settled analysis 2 CU x (DB time + the 300 s tail)', async () => {
    const clock = controlClock(T)
    const lease = await admitted()
    clock.advance(4 * S)
    await settle(lease, 'computed', 2000)
    expect((await day()).cu_s).toBe(608)
  })

  it('does not charge a Render spin-up when Server-Timing reports the DB time', async () => {
    const clock = controlClock(T)
    const lease = await admitted()
    clock.advance(70 * S)
    await settle(lease, 'computed', 2000)
    // DB time began 2 s (+5 s margin) before the end: 2 x (7 + 300).
    expect((await day()).cu_s).toBe(614)
  })

  it('charges the whole wait when the API did not report its DB time', async () => {
    const clock = controlClock(T)
    const lease = await admitted()
    clock.advance(70 * S)
    await settle(lease, 'computed', null)
    expect((await day()).cu_s).toBe(740)
  })

  it('charges a second analysis in the same session only for the tail it extends', async () => {
    const clock = controlClock(T)
    const first = await admitted()
    clock.advance(4 * S)
    await settle(first, 'computed', 2000)
    clock.advance(56 * S)
    const second = await admitted()
    clock.advance(2 * S)
    await settle(second, 'computed', 1000)
    // 608 + 2 x 58 s of extra tail.
    expect((await day()).cu_s).toBe(608 + 116)
  })

  it('charges nothing when the API reported db;dur=0', async () => {
    const clock = controlClock(T)
    const lease = await admitted()
    clock.advance(S)
    await settle(lease, 'computed', 0)
    expect(await day()).toMatchObject({ admitted: 1, cu_s: 0 })
  })

  it.each([
    // kind, admitted, heavy, prefix, refunded, compute charged
    ['not_reached', 0, 0, 0, 1, false],
    ['shed', 0, 0, 0, 1, true],
    ['rejected', 0, 0, 1, 1, true],
    ['reused', 0, 0, 1, 1, true],
    ['computed', 1, 1, 1, 0, true],
  ] as const)(
    'settles %s: admitted %i, heavy %i, prefix %i, refunded %i, compute %s',
    async (kind, admittedCount, heavy, prefix, refunded, charged) => {
      const clock = controlClock(T)
      const lease = await admitted(PREFIX, 'heavy')
      clock.advance(3 * S)
      await settle(lease, kind, null)
      const row = await day()
      expect(row).toMatchObject({ admitted: admittedCount, heavy, refunded })
      expect(await prefixCount()).toBe(prefix)
      expect(row.cu_s > 0).toBe(charged)
      expect(row.egress_bytes > 0).toBe(charged)
      expect(await governorSql('SELECT id FROM leases')).toEqual([])
    },
  )

  it('ignores a second settle and unknown leases', async () => {
    const clock = controlClock(T)
    const lease = await admitted()
    clock.advance(S)
    await settle(lease, 'not_reached')
    await settle(lease, 'not_reached')
    await settle('no-such-lease', 'not_reached')
    expect(await day()).toMatchObject({ admitted: 0, refunded: 1 })
  })

  it("refuses analyses once today's charge would pass (100 x 0.9) / 31 CU-h", async () => {
    controlClock(T)
    const ceilingS = DAILY_CU_HOURS_CONSERVATIVE * 3600
    // A new session is provisionally 2 CU x (90 s + 300 s) = 780 CU-s.
    await governorSql('INSERT INTO days (day, cu_s) VALUES (?, ?)', TODAY, ceilingS - 779)
    expect(await denied()).toMatchObject({
      code: 'daily_capacity_reached',
      scope: 'daily',
      budget: 'cu_day',
      resumesAt: new Date(MIDNIGHT).toISOString(),
    })
    await governorSql('UPDATE days SET cu_s = ? WHERE day = ?', ceilingS - 781, TODAY)
    await admitted()
  })

  it('does not charge concurrent admissions a full session each', async () => {
    const clock = controlClock(T)
    await governorSql(
      'INSERT INTO days (day, cu_s) VALUES (?, ?)',
      TODAY,
      DAILY_CU_HOURS_CONSERVATIVE * 3600 - 1000,
    )
    // Five at once share one provisional window (780 CU-s), not 5 x 780.
    for (let i = 0; i < 5; i += 1) await admitted(`${i}`.repeat(32))
    // 200 s later the window would grow by 400 CU-s: over the remaining 1,000.
    clock.advance(200 * S)
    expect((await denied(OTHER)).budget).toBe('cu_day')
  })

  it('charges a lease that was never settled in full after 10 minutes', async () => {
    const clock = controlClock(T)
    await admitted()
    clock.advance(11 * MIN)
    await admitted(OTHER)
    expect(await governorSql('SELECT prefix FROM leases')).toEqual([{ prefix: OTHER }])
    expect(await day()).toMatchObject({ admitted: 2, cu_s: 780 })
  })
})

describe('ground truth from the Neon API', () => {
  beforeEach(async () => {
    await setNeon({ key: NEON_KEY, project: NEON_PROJECT })
  })

  it('polls once analyses arrive, with the key only in the Authorization header', async () => {
    controlClock(T)
    const calls = mockNeon({ compute_time_seconds: 36_000, data_transfer_bytes: 1e8 })
    await admitted()
    expect(await alarmAt()).toBe(T)
    expect(await runDurableObjectAlarm(governor())).toBe(true)
    expect(calls).toHaveLength(1)
    expect(calls[0]!.url).toBe(`https://console.neon.tech/api/v2/projects/${NEON_PROJECT}`)
    expect(calls[0]!.headers.get('authorization')).toBe(`Bearer ${NEON_KEY}`)
    const report = await governor().budget()
    expect(report.mode).toBe('ground_truth')
    expect(report.compute_today.ceiling_cu_hours).toBe(10)
    expect(report.month).toMatchObject({
      cu_hours: 10,
      egress_bytes: 1e8,
      period_start: new Date(PERIOD_START).toISOString(),
      period_end: new Date(PERIOD_END).toISOString(),
      last_poll_error: null,
    })
    expect(await storedState()).not.toContain(NEON_KEY)
  })

  it('polls at most every 5 minutes and only while analyses keep arriving', async () => {
    const clock = controlClock(T)
    const calls = mockNeon({})
    await admitted()
    await runDurableObjectAlarm(governor())
    clock.advance(MIN)
    await admitted()
    expect(await alarmAt()).toBe(T + 5 * MIN)
    clock.set(T + 5 * MIN)
    await runDurableObjectAlarm(governor())
    expect(calls).toHaveLength(2)
    // No analysis since: the next alarm does not poll, and only the midnight purge remains.
    expect(await alarmAt()).toBe(T + 10 * MIN)
    clock.set(T + 10 * MIN)
    await runDurableObjectAlarm(governor())
    expect(calls).toHaveLength(2)
    expect(await alarmAt()).toBe(MIDNIGHT)
  })

  it('pauses analyses above the pacing line until the allowance catches up', async () => {
    controlClock(T)
    mockNeon({ compute_time_seconds: 70 * 3600 })
    await admitted()
    await runDurableObjectAlarm(governor())
    // Oct 20 12:00 of Oct 1 - Nov 1: allowance 10 + 75 x 0.629 = 57.2 CU-h < 70.
    const denial = await denied(OTHER)
    expect(denial).toMatchObject({
      code: 'capacity_paused',
      scope: 'monthly',
      budget: 'cu_month',
    })
    const need = Date.parse(denial.resumesAt)
    expect(need).toBeGreaterThan(T)
    expect(need).toBeLessThan(PERIOD_END)
    // The in-flight lease and the candidate share one provisional 390 s window (780 CU-s).
    const expected = pacingResumesAt(70 + 780 / 3600, PERIOD_START, PERIOD_END)
    expect(Math.abs(need - expected)).toBeLessThanOrEqual(1)
    expect(denial.retryAfterSeconds).toBe(Math.ceil((need - T) / 1000))
  })

  it('admits below the pacing line', async () => {
    controlClock(T)
    mockNeon({ compute_time_seconds: 40 * 3600 })
    await admitted()
    await runDurableObjectAlarm(governor())
    await admitted(OTHER)
  })

  it('stops at 85 CU-h until the period ends', async () => {
    controlClock(PERIOD_END - DAY_MS / 4)
    mockNeon({ compute_time_seconds: 85 * 3600 })
    await admitted()
    await runDurableObjectAlarm(governor())
    expect(await denied(OTHER)).toMatchObject({
      code: 'capacity_paused',
      budget: 'cu_month',
      resumesAt: new Date(PERIOD_END).toISOString(),
    })
  })

  it('stops at 4.5 GB of egress until the period ends', async () => {
    controlClock(T)
    mockNeon({ data_transfer_bytes: 4.5e9 - 10_000 })
    await admitted()
    await runDurableObjectAlarm(governor())
    expect(await denied(OTHER)).toMatchObject({
      code: 'capacity_paused',
      scope: 'monthly',
      budget: 'egress',
      resumesAt: new Date(PERIOD_END).toISOString(),
    })
  })

  it('bridges the charges made since the previous poll', async () => {
    const clock = controlClock(T)
    mockNeon({ compute_time_seconds: 3600 })
    const first = await admitted()
    await runDurableObjectAlarm(governor())
    clock.advance(4 * S)
    await settle(first, 'computed', 2000)
    // Neon lags: the next poll still reports 1 CU-h.
    clock.set(T + 5 * MIN)
    await admitted()
    await runDurableObjectAlarm(governor())
    const report = await governor().budget()
    expect(report.month.cu_hours).toBe(1)
    // 608 CU-s since the previous poll, plus the in-flight lease's provisional window
    // beyond the charged one: [T+300 s, T+690 s] after T+304 s = 386 s at 2 CU.
    expect(report.month.bridge_cu_hours).toBeCloseTo((608 + 772) / 3600, 4)
  })

  it('drops to the conservative daily ceiling when ground truth is over a day old', async () => {
    const clock = controlClock(T)
    mockNeon({})
    await admitted()
    await runDurableObjectAlarm(governor())
    clock.advance(25 * 60 * MIN)
    const report = await governor().budget()
    expect(report.mode).toBe('conservative')
    expect(report.compute_today.ceiling_cu_hours).toBeCloseTo(DAILY_CU_HOURS_CONSERVATIVE, 4)
  })

  it.each([
    [
      'an HTTP error',
      () => new Response('{"message":"unauthorized"}', { status: 401 }),
      'http_401',
    ],
    [
      'a network error',
      () => {
        throw new TypeError('fetch failed')
      },
      'TypeError',
    ],
    ['an invalid body', () => Response.json({ project: { id: 'x' } }), 'invalid_response'],
  ])(
    'records %s without the key and keeps the conservative mode',
    async (_label, answer, error) => {
      controlClock(T)
      mockNeon(answer as () => Response)
      await admitted()
      await runDurableObjectAlarm(governor())
      const report = await governor().budget()
      expect(report.mode).toBe('conservative')
      expect(report.month.last_poll_error).toBe(error)
      expect(await storedState()).not.toContain(NEON_KEY)
    },
  )

  it('never polls without a key', async () => {
    controlClock(T)
    await setNeon(null)
    const calls = mockNeon({})
    await admitted()
    expect(await alarmAt()).toBe(MIDNIGHT)
    await runDurableObjectAlarm(governor())
    expect(calls).toHaveLength(0)
    expect((await governor().budget()).neon_configured).toBe(false)
  })
})

describe('purge and alarms', () => {
  it('purges prefix rows at midnight even without traffic', async () => {
    const clock = controlClock(T)
    await admitted()
    expect(await alarmAt()).toBe(MIDNIGHT)
    clock.set(MIDNIGHT + S)
    await runDurableObjectAlarm(governor())
    expect(await governorSql('SELECT * FROM prefixes')).toEqual([])
    expect(await day(TODAY)).toMatchObject({ admitted: 1 })
  })

  it('keeps 40 days of day totals', async () => {
    controlClock(T)
    await governorSql('INSERT INTO days (day, cu_s) VALUES (?, 1)', utcDay(T - 41 * DAY_MS))
    await governorSql('INSERT INTO days (day, cu_s) VALUES (?, 1)', utcDay(T - 39 * DAY_MS))
    await admitted()
    const days = await governorSql<{ day: string }>('SELECT day FROM days ORDER BY day')
    expect(days.map((row) => row.day)).toEqual([utcDay(T - 39 * DAY_MS), TODAY])
  })
})

describe('budget report', () => {
  it('reports counts, compute and limits without prefix hashes', async () => {
    const clock = controlClock(T)
    const first = await admitted(PREFIX, 'heavy')
    await admitted(PREFIX)
    clock.advance(4 * S)
    await settle(first, 'computed', 2000)
    await governorSql('INSERT INTO days (day, cu_s) VALUES (?, 3600)', utcDay(T - 2 * DAY_MS))
    const report = await governor().budget()
    expect(report).toMatchObject({
      day: TODAY,
      resets_at: new Date(MIDNIGHT).toISOString(),
      mode: 'conservative',
      neon_configured: false,
      analyses: {
        admitted: 2,
        limit: 600,
        heavy: 1,
        heavy_limit: 300,
        in_flight: 1,
        refunded: 0,
      },
      prefixes: { active: 1, max_admitted: 2, limit: 60 },
    })
    expect(report.compute_rolling_31d_cu_hours).toBeCloseTo((3600 + 608) / 3600, 4)
    expect(report.compute_today.settled_cu_hours).toBeCloseTo(608 / 3600, 4)
    // The second lease's window beyond the charged one: [T+304 s, T+390 s].
    expect(report.compute_today.provisional_cu_hours).toBeCloseTo((2 * 86) / 3600, 4)
    expect(report.utilization.global).toBeCloseTo(2 / 600, 4)
    expect(report.utilization.prefix).toBeCloseTo(2 / 60, 4)
    expect(report.utilization.cu_month).toBeNull()
    expect(JSON.stringify(report)).not.toContain(PREFIX)
  })
})
