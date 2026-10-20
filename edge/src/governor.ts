/**
 * Quota governor Durable Object (ADR-011 "Quota governor"; ADR-015).
 *
 * One SQLite-backed instance named "global" admits the analyses that would
 * reach Render and Neon: uncached analysis POSTs only (static files and KV
 * hits never call it). For each, the Worker calls
 *
 *     admit({prefix, costClass})  ->  {ok: true, leaseId} | a denial
 *     settle(leaseId, {kind, dbMs})          when the origin call ends
 *
 * Admission charges provisionally (counts, plus a compute window sized for
 * the API's 90 s deadline); settling replaces that with the real charge, or
 * refunds it when the API never computed (origin waking or unreachable, or
 * the API shed the request). Budgets, checked in this order:
 *
 *   1. per client IP prefix: 60 per UTC day           429 rate_limited (client)
 *   2. global: 600 per UTC day                        503 daily_capacity_reached (global)
 *   3. heavy analyses: 300 per UTC day                503 daily_capacity_reached (daily)
 *   4. Neon compute today, charged by the model in budget.ts:
 *      10 CU-h with fresh ground truth, else 90/31   503 daily_capacity_reached (daily)
 *   5. with ground truth from the Neon API (optional key):
 *      egress month to date < 4.5 GB                 503 capacity_paused (monthly)
 *      CU-h month to date < min(85, 10 + 75 x elapsed share of the period)
 *                                                    503 capacity_paused (monthly)
 *
 * Ground truth: when `NEON_API_KEY` and `NEON_PROJECT_ID` are set, an alarm
 * reads `GET /projects/{id}` at most every 5 minutes and only while analyses
 * arrived in the last 5 minutes (the compute is awake then anyway). Usage
 * since the previous poll is bridged with the same conservative charges.
 *
 * Privacy: the governor never sees an IP. `prefix` is an HMAC of the /24 or
 * /48 under a key that rotates every UTC day (computed by the Worker), and
 * prefix rows are deleted at the first event after 00:00 UTC; a midnight
 * alarm makes sure that happens even without traffic.
 *
 * Quota frugality: a denial reads a few rows and writes none, so a flood of
 * denied requests cannot spend the Durable Object's daily row-write budget.
 */

import { DurableObject } from 'cloudflare:workers'
import {
  cuSeconds,
  dbStart,
  elapsedShare,
  extendWindow,
  nextUtcMidnight,
  pacingAllowance,
  pacingResumesAt,
  provisionalInterval,
  secondsUntil,
  unionLengthAfter,
  utcDay,
  DAY_MS,
  type Interval,
} from './budget'
import {
  DAILY_CU_HOURS_CONSERVATIVE,
  DAILY_CU_HOURS_GROUND_TRUTH,
  EGRESS_ESTIMATE_BYTES,
  EGRESS_STOP_BYTES,
  GLOBAL_DAILY_ANALYSES,
  GROUND_TRUTH_MAX_AGE_MS,
  HEAVY_DAILY_ANALYSES,
  LEASE_TTL_MS,
  NEON_API_BASE,
  NEON_POLL_INTERVAL_MS,
  NEON_POLL_TIMEOUT_MS,
  PREFIX_DAILY_ANALYSES,
} from './constants'

/** The one instance every Worker isolate talks to. */
export const GOVERNOR_NAME = 'global'

/** Day rows kept for the ops report's rolling 31-day sum. */
const DAY_RETENTION_DAYS = 40

export type CostClass = 'light' | 'heavy'

export interface AdmitRequest {
  /** HMAC of the client's IP prefix under today's key; never an IP. */
  prefix: string
  costClass: CostClass
}

export type DenialCode = 'rate_limited' | 'daily_capacity_reached' | 'capacity_paused'
export type DenialScope = 'client' | 'global' | 'daily' | 'monthly'
export type BudgetName = 'prefix' | 'global' | 'heavy' | 'cu_day' | 'cu_month' | 'egress'

export interface Admitted {
  ok: true
  leaseId: string
}

export interface Denied {
  ok: false
  code: DenialCode
  scope: DenialScope
  budget: BudgetName
  retryAfterSeconds: number
  /** ISO-8601 time the budget allows this request again. */
  resumesAt: string
}

export type AdmitResult = Admitted | Denied

/**
 * How an admitted analysis ended, as the Worker saw it:
 *
 * - `not_reached`: the API never answered (Render waking, platform error,
 *   connection failure, open breaker). Fully refunded.
 * - `shed`: the API refused it without computing (`overloaded`,
 *   `rate_limited`, `daily_capacity_reached`). Counts refunded.
 * - `rejected`: a 4xx from the API (validation). Global and heavy counts
 *   refunded; the prefix keeps the charge, so invalid requests stay bounded.
 * - `reused`: a 2xx the API served from its cache or a joined computation.
 *   Refunded like `rejected`.
 * - `computed`: everything else (2xx miss or bypass, timeouts, DB errors).
 *
 * Compute is charged for everything the API answered unless its
 * `Server-Timing` reported `db;dur=0` (`dbMs === 0`).
 */
export type SettleKind = 'not_reached' | 'shed' | 'rejected' | 'reused' | 'computed'

export interface SettleReport {
  kind: SettleKind
  /** `Server-Timing: db;dur=` in ms, or null when the API did not report it. */
  dbMs: number | null
}

/** Month-to-date usage from Neon's project endpoint, with the bridge bases. */
interface NeonUsage {
  polledAt: number
  computeTimeSeconds: number
  dataTransferBytes: number
  periodStart: number
  periodEnd: number
  /** Bridge counters at the previous successful poll: usage is bridged from there. */
  cuBaseS: number
  egressBase: number
  /** Bridge counters at this poll (the next poll's bases). */
  cuAtPollS: number
  egressAtPoll: number
}

/** Everything that is not a per-day or per-lease row, kept in one row. */
interface GovernorState {
  /** End of the compute window already charged by settled analyses (epoch ms). */
  chargedUntil: number
  /** Every CU-second and egress byte ever charged: the bridge counters. */
  cuTotalS: number
  egressTotal: number
  lastAdmitAt: number
  /** Last Neon poll attempt, successful or not. */
  lastPollAt: number
  /** Why the last poll failed (`http_401`, `TimeoutError`, ...), or null. */
  pollError: string | null
  /** The UTC day whose purge has run. */
  purgedDay: string
  /** The alarm this object last scheduled, or null when none is pending. */
  alarmAt: number | null
  neon: NeonUsage | null
}

const INITIAL_STATE: GovernorState = {
  chargedUntil: 0,
  cuTotalS: 0,
  egressTotal: 0,
  lastAdmitAt: 0,
  lastPollAt: 0,
  pollError: null,
  purgedDay: '',
  alarmAt: null,
  neon: null,
}

type DayRow = {
  admitted: number
  heavy: number
  cu_s: number
  egress_bytes: number
  refunded: number
}

type LeaseRow = {
  id: string
  day: string
  prefix: string
  heavy: number
  admitted_at: number
  egress_bytes: number
}

/** The ops report (`GET /api/_ops/budget`). Contains no IPs and no prefix hashes. */
export interface BudgetReport {
  generated_at: string
  day: string
  resets_at: string
  mode: 'ground_truth' | 'conservative'
  neon_configured: boolean
  analyses: {
    admitted: number
    limit: number
    heavy: number
    heavy_limit: number
    in_flight: number
    refunded: number
  }
  prefixes: { active: number; max_admitted: number; limit: number }
  compute_today: {
    settled_cu_hours: number
    provisional_cu_hours: number
    ceiling_cu_hours: number
  }
  compute_rolling_31d_cu_hours: number
  month: {
    polled_at: string | null
    last_poll_error: string | null
    period_start: string | null
    period_end: string | null
    elapsed_share: number | null
    cu_hours: number | null
    bridge_cu_hours: number | null
    allowance_cu_hours: number | null
    egress_bytes: number | null
    bridge_egress_bytes: number | null
    egress_stop_bytes: number
  }
  /** Used / limit for each budget (the daily quota report alerts above 0.7 of pro-rated). */
  utilization: Record<BudgetName, number | null>
}

export class Governor extends DurableObject<Env> {
  private readonly sql: SqlStorage

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env)
    this.sql = ctx.storage.sql
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS days (
        day TEXT PRIMARY KEY,
        admitted INTEGER NOT NULL DEFAULT 0,
        heavy INTEGER NOT NULL DEFAULT 0,
        cu_s REAL NOT NULL DEFAULT 0,
        egress_bytes REAL NOT NULL DEFAULT 0,
        refunded INTEGER NOT NULL DEFAULT 0
      );
      CREATE TABLE IF NOT EXISTS prefixes (
        day TEXT NOT NULL,
        prefix TEXT NOT NULL,
        count INTEGER NOT NULL,
        PRIMARY KEY (day, prefix)
      );
      CREATE TABLE IF NOT EXISTS leases (
        id TEXT PRIMARY KEY,
        day TEXT NOT NULL,
        prefix TEXT NOT NULL,
        heavy INTEGER NOT NULL,
        admitted_at INTEGER NOT NULL,
        egress_bytes REAL NOT NULL
      );
      CREATE TABLE IF NOT EXISTS state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        value TEXT NOT NULL
      );
    `)
  }

  // --- RPC ---------------------------------------------------------------

  async admit(request: AdmitRequest): Promise<AdmitResult> {
    const now = Date.now()
    const today = utcDay(now)
    const midnight = nextUtcMidnight(now)
    // Anything but an explicit 'light' is charged as heavy.
    const costClass: CostClass = request.costClass === 'light' ? 'light' : 'heavy'
    const heavy = costClass === 'heavy'

    // Cheapest checks first: a flood of denials reads one or two rows.
    if (this.prefixCount(today, request.prefix) >= PREFIX_DAILY_ANALYSES) {
      return deny('rate_limited', 'client', 'prefix', midnight, now)
    }
    let day = this.dayRow(today)
    if (day.admitted >= GLOBAL_DAILY_ANALYSES) {
      return deny('daily_capacity_reached', 'global', 'global', midnight, now)
    }
    if (heavy && day.heavy >= HEAVY_DAILY_ANALYSES) {
      return deny('daily_capacity_reached', 'daily', 'heavy', midnight, now)
    }

    const state = this.loadState()
    if (this.expireLeases(now, state)) {
      this.saveState(state)
      day = this.dayRow(today)
    }
    const leases = this.leaseRows()
    const candidate = provisionalInterval(now)

    const todays = leases.filter((lease) => lease.day === today).map(leaseInterval)
    const dayCuS =
      day.cu_s + cuSeconds(unionLengthAfter([...todays, candidate], state.chargedUntil))
    if (dayCuS > dailyCeilingCuHours(state, now, this.neonConfigured()) * 3600) {
      return deny('daily_capacity_reached', 'daily', 'cu_day', midnight, now)
    }

    const neon = currentNeon(state, now)
    if (neon !== null) {
      const inFlightEgress = leases.reduce((sum, lease) => sum + lease.egress_bytes, 0)
      const egress =
        neon.dataTransferBytes +
        (state.egressTotal - neon.egressBase) +
        inFlightEgress +
        EGRESS_ESTIMATE_BYTES[costClass]
      if (egress >= EGRESS_STOP_BYTES) {
        return deny('capacity_paused', 'monthly', 'egress', neon.periodEnd, now)
      }
      const provisionalS = cuSeconds(
        unionLengthAfter([...leases.map(leaseInterval), candidate], state.chargedUntil),
      )
      const monthCuHours =
        (neon.computeTimeSeconds + (state.cuTotalS - neon.cuBaseS) + provisionalS) / 3600
      const allowance = pacingAllowance(elapsedShare(now, neon.periodStart, neon.periodEnd))
      if (monthCuHours > allowance) {
        const resumesAt = pacingResumesAt(monthCuHours, neon.periodStart, neon.periodEnd)
        return deny('capacity_paused', 'monthly', 'cu_month', resumesAt, now)
      }
    }

    const leaseId = crypto.randomUUID()
    this.sql.exec(
      'INSERT INTO leases (id, day, prefix, heavy, admitted_at, egress_bytes) VALUES (?, ?, ?, ?, ?, ?)',
      leaseId,
      today,
      request.prefix,
      heavy ? 1 : 0,
      now,
      EGRESS_ESTIMATE_BYTES[costClass],
    )
    this.sql.exec(
      `INSERT INTO days (day, admitted, heavy) VALUES (?, 1, ?)
       ON CONFLICT (day) DO UPDATE SET admitted = admitted + 1, heavy = heavy + excluded.heavy`,
      today,
      heavy ? 1 : 0,
    )
    this.sql.exec(
      `INSERT INTO prefixes (day, prefix, count) VALUES (?, ?, 1)
       ON CONFLICT (day, prefix) DO UPDATE SET count = count + 1`,
      today,
      request.prefix,
    )
    state.lastAdmitAt = now
    if (state.purgedDay !== today) this.purge(now, state)
    await this.scheduleAlarm(state, now)
    return { ok: true, leaseId }
  }

  async settle(leaseId: string, report: SettleReport): Promise<void> {
    const lease = this.sql
      .exec<LeaseRow>('SELECT * FROM leases WHERE id = ?', leaseId)
      .toArray()[0]
    // Unknown or already expired (and charged in full): nothing to do.
    if (lease === undefined) return
    const now = Date.now()
    this.sql.exec('DELETE FROM leases WHERE id = ?', leaseId)

    const { kind } = report
    const refundCounts = kind !== 'computed'
    const refundPrefix = kind === 'not_reached' || kind === 'shed'
    if (refundCounts) {
      this.sql.exec(
        `UPDATE days SET admitted = MAX(0, admitted - 1), heavy = MAX(0, heavy - ?),
         refunded = refunded + 1 WHERE day = ?`,
        lease.heavy,
        lease.day,
      )
    }
    if (refundPrefix) {
      this.sql.exec(
        'UPDATE prefixes SET count = MAX(0, count - 1) WHERE day = ? AND prefix = ?',
        lease.day,
        lease.prefix,
      )
    }
    const dbTouched = kind !== 'not_reached' && report.dbMs !== 0
    if (!dbTouched) return

    const state = this.loadState()
    const start = dbStart(lease.admitted_at, now, report.dbMs)
    const window = extendWindow(state.chargedUntil, start, now)
    this.charge(state, lease.day, cuSeconds(window.addedMs), lease.egress_bytes)
    state.chargedUntil = window.chargedUntil
    this.saveState(state)
  }

  async budget(): Promise<BudgetReport> {
    const now = Date.now()
    const today = utcDay(now)
    const state = this.loadState()
    const day = this.dayRow(today)
    const leases = this.leaseRows()
    const prefixes = this.sql
      .exec<{ active: number; max_admitted: number | null }>(
        'SELECT count(*) AS active, max(count) AS max_admitted FROM prefixes WHERE day = ?',
        today,
      )
      .one()
    const rolling = this.sql
      .exec<{ cu_s: number | null }>(
        'SELECT sum(cu_s) AS cu_s FROM days WHERE day > ?',
        utcDay(now - 31 * DAY_MS),
      )
      .one()

    const todays = leases.filter((lease) => lease.day === today).map(leaseInterval)
    const provisionalTodayS = cuSeconds(unionLengthAfter(todays, state.chargedUntil))
    const ceiling = dailyCeilingCuHours(state, now, this.neonConfigured())
    const neon = currentNeon(state, now)

    let month: BudgetReport['month'] = {
      polled_at: state.neon === null ? null : new Date(state.neon.polledAt).toISOString(),
      last_poll_error: state.pollError,
      period_start: null,
      period_end: null,
      elapsed_share: null,
      cu_hours: null,
      bridge_cu_hours: null,
      allowance_cu_hours: null,
      egress_bytes: null,
      bridge_egress_bytes: null,
      egress_stop_bytes: EGRESS_STOP_BYTES,
    }
    let monthUse: { cu: number; allowance: number; egress: number } | null = null
    if (neon !== null) {
      const share = elapsedShare(now, neon.periodStart, neon.periodEnd)
      const provisionalS = cuSeconds(
        unionLengthAfter(leases.map(leaseInterval), state.chargedUntil),
      )
      const bridgeCuHours = (state.cuTotalS - neon.cuBaseS + provisionalS) / 3600
      const bridgeEgress =
        state.egressTotal -
        neon.egressBase +
        leases.reduce((sum, lease) => sum + lease.egress_bytes, 0)
      const allowance = pacingAllowance(share)
      monthUse = {
        cu: neon.computeTimeSeconds / 3600 + bridgeCuHours,
        allowance,
        egress: neon.dataTransferBytes + bridgeEgress,
      }
      month = {
        ...month,
        period_start: new Date(neon.periodStart).toISOString(),
        period_end: new Date(neon.periodEnd).toISOString(),
        elapsed_share: round(share, 4),
        cu_hours: round(neon.computeTimeSeconds / 3600, 4),
        bridge_cu_hours: round(bridgeCuHours, 4),
        allowance_cu_hours: round(allowance, 4),
        egress_bytes: neon.dataTransferBytes,
        bridge_egress_bytes: Math.round(bridgeEgress),
      }
    }
    const dayCuHours = (day.cu_s + provisionalTodayS) / 3600
    return {
      generated_at: new Date(now).toISOString(),
      day: today,
      resets_at: new Date(nextUtcMidnight(now)).toISOString(),
      mode: groundTruthFresh(state, now, this.neonConfigured())
        ? 'ground_truth'
        : 'conservative',
      neon_configured: this.neonConfigured(),
      analyses: {
        admitted: day.admitted,
        limit: GLOBAL_DAILY_ANALYSES,
        heavy: day.heavy,
        heavy_limit: HEAVY_DAILY_ANALYSES,
        in_flight: leases.length,
        refunded: day.refunded,
      },
      prefixes: {
        active: prefixes.active,
        max_admitted: prefixes.max_admitted ?? 0,
        limit: PREFIX_DAILY_ANALYSES,
      },
      compute_today: {
        settled_cu_hours: round(day.cu_s / 3600, 4),
        provisional_cu_hours: round(provisionalTodayS / 3600, 4),
        ceiling_cu_hours: round(ceiling, 4),
      },
      compute_rolling_31d_cu_hours: round((rolling.cu_s ?? 0) / 3600, 4),
      month,
      utilization: {
        prefix: round((prefixes.max_admitted ?? 0) / PREFIX_DAILY_ANALYSES, 4),
        global: round(day.admitted / GLOBAL_DAILY_ANALYSES, 4),
        heavy: round(day.heavy / HEAVY_DAILY_ANALYSES, 4),
        cu_day: round(dayCuHours / ceiling, 4),
        cu_month: monthUse === null ? null : round(monthUse.cu / monthUse.allowance, 4),
        egress: monthUse === null ? null : round(monthUse.egress / EGRESS_STOP_BYTES, 4),
      },
    }
  }

  /** Midnight purge, lease expiry and the Neon usage poll. */
  override async alarm(): Promise<void> {
    const now = Date.now()
    const state = this.loadState()
    state.alarmAt = null
    this.expireLeases(now, state)
    if (state.purgedDay !== utcDay(now)) this.purge(now, state)
    const poll = this.pollDue(state, now)
    if (poll) state.lastPollAt = now
    this.saveState(state)
    if (poll) await this.pollNeon(now)
    const latest = this.loadState()
    await this.scheduleAlarm(latest, Date.now())
  }

  // --- storage -----------------------------------------------------------

  private loadState(): GovernorState {
    const row = this.sql
      .exec<{ value: string }>('SELECT value FROM state WHERE id = 1')
      .toArray()[0]
    if (row === undefined) return { ...INITIAL_STATE }
    return { ...INITIAL_STATE, ...(JSON.parse(row.value) as Partial<GovernorState>) }
  }

  private saveState(state: GovernorState): void {
    this.sql.exec(
      `INSERT INTO state (id, value) VALUES (1, ?)
       ON CONFLICT (id) DO UPDATE SET value = excluded.value`,
      JSON.stringify(state),
    )
  }

  private prefixCount(day: string, prefix: string): number {
    const row = this.sql
      .exec<{ count: number }>(
        'SELECT count FROM prefixes WHERE day = ? AND prefix = ?',
        day,
        prefix,
      )
      .toArray()[0]
    return row?.count ?? 0
  }

  private dayRow(day: string): DayRow {
    const row = this.sql
      .exec<DayRow & { day: string }>('SELECT * FROM days WHERE day = ?', day)
      .toArray()[0]
    return row ?? { admitted: 0, heavy: 0, cu_s: 0, egress_bytes: 0, refunded: 0 }
  }

  private leaseRows(): LeaseRow[] {
    return this.sql.exec<LeaseRow>('SELECT * FROM leases').toArray()
  }

  /** Add settled charges to a day and to the bridge counters (`state` is saved by the caller). */
  private charge(state: GovernorState, day: string, cuS: number, egressBytes: number): void {
    this.sql.exec(
      `INSERT INTO days (day, cu_s, egress_bytes) VALUES (?, ?, ?)
       ON CONFLICT (day) DO UPDATE SET cu_s = cu_s + excluded.cu_s,
         egress_bytes = egress_bytes + excluded.egress_bytes`,
      day,
      cuS,
      egressBytes,
    )
    state.cuTotalS += cuS
    state.egressTotal += egressBytes
  }

  /**
   * Charge in full, and drop, every lease older than `LEASE_TTL_MS`: its
   * Worker invocation died before settling. Returns whether any expired.
   */
  private expireLeases(now: number, state: GovernorState): boolean {
    const stale = this.sql
      .exec<LeaseRow>('SELECT * FROM leases WHERE admitted_at < ?', now - LEASE_TTL_MS)
      .toArray()
    for (const lease of stale) {
      const [start, end] = leaseInterval(lease)
      this.charge(state, lease.day, cuSeconds(end - start), lease.egress_bytes)
      state.chargedUntil = Math.max(state.chargedUntil, end)
      this.sql.exec('DELETE FROM leases WHERE id = ?', lease.id)
    }
    return stale.length > 0
  }

  /** Delete earlier days' prefix rows (their key has rotated) and old day rows. */
  private purge(now: number, state: GovernorState): void {
    const today = utcDay(now)
    this.sql.exec('DELETE FROM prefixes WHERE day < ?', today)
    this.sql.exec('DELETE FROM days WHERE day < ?', utcDay(now - DAY_RETENTION_DAYS * DAY_MS))
    state.purgedDay = today
  }

  // --- alarm and the Neon poll --------------------------------------------

  private neonConfigured(): boolean {
    return (
      Boolean(this.env.NEON_API_KEY) && /^[A-Za-z0-9-]{1,64}$/.test(this.env.NEON_PROJECT_ID)
    )
  }

  /** A poll is due while analyses arrived in the last interval and the last poll is that old. */
  private pollDue(state: GovernorState, now: number): boolean {
    return (
      this.neonConfigured() &&
      now - state.lastAdmitAt <= NEON_POLL_INTERVAL_MS &&
      now - state.lastPollAt >= NEON_POLL_INTERVAL_MS
    )
  }

  /**
   * Keep one alarm pending at the earliest of: the next poll (while traffic
   * is recent) and the next midnight purge (while prefix rows or leases
   * exist). Saves `state`.
   */
  private async scheduleAlarm(state: GovernorState, now: number): Promise<void> {
    const times: number[] = []
    if (this.neonConfigured() && now - state.lastAdmitAt <= NEON_POLL_INTERVAL_MS) {
      times.push(Math.max(now, state.lastPollAt + NEON_POLL_INTERVAL_MS))
    }
    // EXISTS reads at most one row per table (count(*) would read them all).
    const pending = this.sql
      .exec<{ n: number }>(
        'SELECT EXISTS (SELECT 1 FROM prefixes) OR EXISTS (SELECT 1 FROM leases) AS n',
      )
      .one()
    if (pending.n) times.push(nextUtcMidnight(now))
    const wanted = times.length > 0 ? Math.min(...times) : null
    const current = state.alarmAt !== null && state.alarmAt >= now ? state.alarmAt : null
    if (wanted === null || (current !== null && current <= wanted)) {
      this.saveState(state)
      return
    }
    state.alarmAt = wanted
    this.saveState(state)
    await this.ctx.storage.setAlarm(wanted)
  }

  /**
   * Read month-to-date usage from Neon. The key goes only into the
   * Authorization header; failures record a status or error name, never a
   * message or the URL. Other events may run while the fetch is pending, so
   * the state is re-read before the result is applied.
   */
  private async pollNeon(now: number): Promise<void> {
    let result: ParsedUsage | string
    try {
      const response = await fetch(
        `${NEON_API_BASE}/projects/${encodeURIComponent(this.env.NEON_PROJECT_ID)}`,
        {
          headers: {
            Authorization: `Bearer ${this.env.NEON_API_KEY}`,
            Accept: 'application/json',
          },
          signal: AbortSignal.timeout(NEON_POLL_TIMEOUT_MS),
        },
      )
      if (response.ok) {
        result = parseNeonProject(await response.json()) ?? 'invalid_response'
      } else {
        result = `http_${response.status}`
        await response.body?.cancel()
      }
    } catch (err) {
      result = err instanceof Error ? err.name : 'Error'
    }

    const state = this.loadState()
    if (typeof result === 'string') {
      state.pollError = result
    } else {
      const previous = state.neon
      // First poll: re-count today's charges too (Neon's figures lag).
      const today = this.dayRow(utcDay(now))
      state.pollError = null
      state.neon = {
        ...result,
        polledAt: now,
        cuBaseS: previous?.cuAtPollS ?? Math.max(0, state.cuTotalS - today.cu_s),
        egressBase:
          previous?.egressAtPoll ?? Math.max(0, state.egressTotal - today.egress_bytes),
        cuAtPollS: state.cuTotalS,
        egressAtPoll: state.egressTotal,
      }
    }
    this.saveState(state)
  }
}

// --- helpers ------------------------------------------------------------------

function deny(
  code: DenialCode,
  scope: DenialScope,
  budget: BudgetName,
  resumesAt: number,
  now: number,
): Denied {
  return {
    ok: false,
    code,
    scope,
    budget,
    retryAfterSeconds: secondsUntil(resumesAt, now),
    resumesAt: new Date(resumesAt).toISOString(),
  }
}

function leaseInterval(lease: LeaseRow): Interval {
  return provisionalInterval(lease.admitted_at)
}

function round(value: number, digits: number): number {
  const factor = 10 ** digits
  return Math.round(value * factor) / factor
}

/** Ground truth for the current consumption period, however old. */
function currentNeon(state: GovernorState, now: number): NeonUsage | null {
  return state.neon !== null && now < state.neon.periodEnd ? state.neon : null
}

/** Ground truth recent enough to earn the larger daily ceiling. */
function groundTruthFresh(state: GovernorState, now: number, configured: boolean): boolean {
  return (
    configured &&
    currentNeon(state, now) !== null &&
    now - state.neon!.polledAt <= GROUND_TRUTH_MAX_AGE_MS
  )
}

function dailyCeilingCuHours(state: GovernorState, now: number, configured: boolean): number {
  return groundTruthFresh(state, now, configured)
    ? DAILY_CU_HOURS_GROUND_TRUTH
    : DAILY_CU_HOURS_CONSERVATIVE
}

type ParsedUsage = Pick<
  NeonUsage,
  'computeTimeSeconds' | 'dataTransferBytes' | 'periodStart' | 'periodEnd'
>

/** The usage fields of a `GET /projects/{id}` body, or null when they are missing or invalid. */
export function parseNeonProject(payload: unknown): ParsedUsage | null {
  const project = (payload as { project?: Record<string, unknown> } | null)?.project
  if (typeof project !== 'object' || project === null) return null
  const compute = project.compute_time_seconds
  const egress = project.data_transfer_bytes
  const start = Date.parse(String(project.consumption_period_start))
  const end = Date.parse(String(project.consumption_period_end))
  if (typeof compute !== 'number' || !Number.isFinite(compute) || compute < 0) return null
  if (typeof egress !== 'number' || !Number.isFinite(egress) || egress < 0) return null
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return null
  return {
    computeTimeSeconds: compute,
    dataTransferBytes: egress,
    periodStart: start,
    periodEnd: end,
  }
}
