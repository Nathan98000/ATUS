/**
 * Quota governor Durable Object (ADR-011 "Quota governor"; ADR-015).
 *
 * One SQLite-backed instance, named by `IP_HMAC_KEY` (admission.ts,
 * `governorName`), admits what the Worker sends to Render: uncached analysis
 * POSTs, and metadata GETs the static snapshot could not answer (static
 * files, KV hits and snapshot answers never call it). For each analysis, the
 * Worker calls
 *
 *     admit({prefix, costClass})  ->  {ok: true, leaseId} | a denial
 *     settle(leaseId, {kind, dbMs})          when the origin call ends
 *
 * Admission charges provisionally (counts, plus a compute window sized for
 * the API's 90 s deadline); settling replaces that with the real charge, or
 * refunds it when the API never computed (origin waking or unreachable, or
 * the API shed the request). Budgets, checked in this order:
 *
 *   1. per client IP prefix: 60 per UTC day, and 300   429 rate_limited (client)
 *      admissions including refunded ones
 *   2. global: 600 per UTC day                        503 daily_capacity_reached (global)
 *   3. heavy analyses: 300 per UTC day                503 daily_capacity_reached (daily)
 *   4. per client IP prefix: a fifth of check 5's     429 rate_limited (client)
 *      ceiling in compute charged to it today
 *   5. Neon compute today, charged by the model in budget.ts:
 *      10 CU-h with fresh ground truth, else 90/31   503 daily_capacity_reached (daily)
 *   6. without ground truth: Neon egress today        503 daily_capacity_reached (daily)
 *      (estimated) < 4.5 GB / 31
 *   7. with ground truth from the Neon API (optional key):
 *      egress month to date < 4.5 GB                 503 capacity_paused (monthly)
 *      CU-h month to date < min(85, 10 + 75 x elapsed share of the period)
 *                                                    503 capacity_paused (monthly)
 *
 * A metadata lookup (`admitLookup`) is counted: 200 per prefix and 2,000 in
 * all per UTC day, which bounds what metadata costs Render's monthly
 * outbound bandwidth. It also passes checks 4-7 and holds a lease, settled
 * like an analysis's but never refunding counts: the API answers metadata
 * from memory (`db;dur=0`, free), except on a freshly started process's
 * first data request, whose snapshot read can wake Neon. That one is charged
 * its compute window and the snapshot read's egress (review round 2: before,
 * one network sending a lookup after each Render spin-down woke Neon about
 * 90 times a day uncharged, past every compute budget).
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
 * denied requests cannot spend the Durable Object's daily row-write budget;
 * admissions that end refunded (and so never reach the 60) are capped per
 * prefix by `PREFIX_DAILY_ADMISSIONS`.
 *
 * Storage outlives every Worker version, and a version deployed with a
 * leaked Cloudflare token runs code of its own as this class (review round 4,
 * docs/deployment.md "Rotation", item 5). The recovery rotates
 * `IP_HMAC_KEY`, which moves the Worker onto a fresh instance; in any
 * instance, what no version of this code could have written is not trusted:
 * rows dated after today are deleted whenever the instance starts (every
 * deploy restarts it), a count that is not a non-negative number counts as
 * spent, and the state row is checked on every load (`sanitizeState`).
 */

import { DurableObject } from 'cloudflare:workers'
import {
  chargeWindow,
  cuSeconds,
  dbStart,
  elapsedShare,
  mergeIntervals,
  nextUtcMidnight,
  pacingAllowance,
  pacingResumesAt,
  provisionalInterval,
  secondsUntil,
  uncoveredLength,
  utcDay,
  DAY_MS,
  type Interval,
} from './budget'
import {
  DAILY_CU_HOURS_CONSERVATIVE,
  DAILY_CU_HOURS_GROUND_TRUTH,
  EGRESS_DAILY_CONSERVATIVE_BYTES,
  EGRESS_ESTIMATE_BYTES,
  EGRESS_STOP_BYTES,
  GLOBAL_DAILY_ANALYSES,
  GLOBAL_DAILY_LOOKUPS,
  GROUND_TRUTH_MAX_AGE_MS,
  HEAVY_DAILY_ANALYSES,
  LEASE_TTL_MS,
  NEON_API_BASE,
  NEON_IDLE_TAIL_MS,
  NEON_POLL_INTERVAL_MS,
  NEON_POLL_TIMEOUT_MS,
  PREFIX_DAILY_ADMISSIONS,
  PREFIX_DAILY_ANALYSES,
  PREFIX_DAILY_CU_SHARE,
  PREFIX_DAILY_LOOKUPS,
  PROVISIONAL_BUSY_MS,
  SNAPSHOT_READ_EGRESS_BYTES,
} from './constants'

/** Day rows kept for the ops report's rolling 31-day sum. */
const DAY_RETENTION_DAYS = 40

/**
 * How far past the time it is written a charged window can end: a settle
 * charges up to `NEON_IDLE_TAIL_MS` ahead, an expired lease its whole
 * provisional window (less, as it is already past). Anything later is planted.
 */
const MAX_WINDOW_AHEAD_MS = PROVISIONAL_BUSY_MS + NEON_IDLE_TAIL_MS

/**
 * Longest Neon consumption period accepted, and how far after a poll it may
 * end: a calendar month, with a day's slack.
 */
const NEON_MAX_PERIOD_MS = 32 * DAY_MS

/** The largest egress estimate a lease can hold. */
const MAX_LEASE_EGRESS_BYTES = Math.max(
  EGRESS_ESTIMATE_BYTES.light,
  EGRESS_ESTIMATE_BYTES.heavy,
  SNAPSHOT_READ_EGRESS_BYTES,
)

/** Columns the schema gained after its first version: [table, column, definition]. */
const ADDED_COLUMNS: readonly (readonly [string, string, string])[] = [
  ['days', 'lookups', 'INTEGER NOT NULL DEFAULT 0'],
  ['prefixes', 'cu_s', 'REAL NOT NULL DEFAULT 0'],
  ['prefixes', 'lookups', 'INTEGER NOT NULL DEFAULT 0'],
  ['leases', 'lookup', 'INTEGER NOT NULL DEFAULT 0'],
]

export type CostClass = 'light' | 'heavy'

export interface AdmitRequest {
  /** HMAC of the client's IP prefix under today's key; never an IP. */
  prefix: string
  costClass: CostClass
}

export interface LookupRequest {
  /** As for `AdmitRequest`. */
  prefix: string
}

export type DenialCode = 'rate_limited' | 'daily_capacity_reached' | 'capacity_paused'
export type DenialScope = 'client' | 'global' | 'daily' | 'monthly'
export type BudgetName =
  'prefix' | 'global' | 'heavy' | 'cu_day' | 'egress_day' | 'cu_month' | 'egress' | 'metadata'

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

/** As for an analysis: a lookup holds a lease until the Worker settles it. */
export type LookupResult = AdmitResult

/**
 * How an admitted analysis ended, as the Worker saw it:
 *
 * - `not_reached`: the API never took the request (Render waking, a
 *   platform error page other than a 524, connection failure, open breaker).
 *   Fully refunded.
 * - `shed`: the API refused it without computing (`overloaded`,
 *   `rate_limited`, `daily_capacity_reached`, and the data-availability
 *   refusals: `database_paused`, `database_unavailable`,
 *   `data_version_mismatch`, `data_not_loaded`, `schema_not_initialized`).
 *   Counts refunded.
 * - `rejected`: a 4xx from the API (validation). Global and heavy counts
 *   refunded; the prefix keeps the charge, so invalid requests stay bounded.
 * - `reused`: a 2xx the API served from its cache or a joined computation.
 *   Refunded like `rejected`.
 * - `computed`: everything else (2xx miss or bypass, timeouts — including
 *   Cloudflare's 524, after which the API still runs the analysis to the
 *   end — and unexpected errors).
 *
 * A `rejected` or `reused` answer that reported DB time (`dbMs > 0`) keeps
 * its global and heavy counts: a 422 found only by the statistics query
 * (`insufficient_data`) ran that query in full, and the heavy count is what
 * bounds the Neon egress such queries cost.
 *
 * Compute is charged for everything the API answered unless its
 * `Server-Timing` reported `db;dur=0` (`dbMs === 0`), to the day and to the
 * client's prefix.
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
  /**
   * Compute windows already charged (epoch ms; sorted, disjoint), kept back
   * to the oldest time a live lease can reach: `LEASE_TTL_MS` ago.
   */
  charged: Interval[]
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
  charged: [],
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
  lookups: number
}

type PrefixRow = {
  /** Analyses charged (the 60). */
  count: number
  /** Admissions, refunded ones included (the 300). */
  attempts: number
  /** Compute charged to this prefix's analyses, in CU-s. */
  cu_s: number
  /** Metadata lookups sent to the origin. */
  lookups: number
}

type LeaseRow = {
  id: string
  day: string
  prefix: string
  heavy: number
  admitted_at: number
  egress_bytes: number
  /** 1 for a metadata lookup's lease: its settle refunds no counts. */
  lookup: number
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
  prefixes: {
    active: number
    max_admitted: number
    limit: number
    /** Largest number of admissions by one prefix today, refunded ones included. */
    max_attempts: number
    attempts_limit: number
    /** Largest settled compute charged to one prefix today. */
    max_cu_hours: number
    /** The per-prefix share of today's compute ceiling. */
    cu_limit_cu_hours: number
  }
  compute_today: {
    settled_cu_hours: number
    provisional_cu_hours: number
    ceiling_cu_hours: number
  }
  /** Estimated Neon egress today; the ceiling applies only without ground truth (null then). */
  egress_today: {
    settled_bytes: number
    provisional_bytes: number
    ceiling_bytes: number | null
  }
  /** Metadata GETs sent to the origin today (those the snapshot could not answer). */
  metadata: {
    origin_calls: number
    limit: number
    max_prefix: number
    prefix_limit: number
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
        refunded INTEGER NOT NULL DEFAULT 0,
        lookups INTEGER NOT NULL DEFAULT 0
      );
      CREATE TABLE IF NOT EXISTS prefixes (
        day TEXT NOT NULL,
        prefix TEXT NOT NULL,
        count INTEGER NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        cu_s REAL NOT NULL DEFAULT 0,
        lookups INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day, prefix)
      );
      CREATE TABLE IF NOT EXISTS leases (
        id TEXT PRIMARY KEY,
        day TEXT NOT NULL,
        prefix TEXT NOT NULL,
        heavy INTEGER NOT NULL,
        admitted_at INTEGER NOT NULL,
        egress_bytes REAL NOT NULL,
        lookup INTEGER NOT NULL DEFAULT 0
      );
      CREATE TABLE IF NOT EXISTS state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        value TEXT NOT NULL
      );
    `)
    // Columns added after the first local runs (`wrangler dev` keeps its
    // database under .wrangler/): an older database gains them here.
    for (const [table, column, type] of ADDED_COLUMNS) {
      const present = this.sql
        .exec<{ name: string }>(`PRAGMA table_info(${table})`)
        .toArray()
        .some((row) => row.name === column)
      if (!present) this.sql.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${type}`)
    }
    this.dropImpossibleRows(Date.now())
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
    const prefixUse = this.prefixUse(today, request.prefix)
    if (
      prefixUse.count >= PREFIX_DAILY_ANALYSES ||
      prefixUse.attempts >= PREFIX_DAILY_ADMISSIONS
    ) {
      return deny('rate_limited', 'client', 'prefix', midnight, now)
    }
    const day = this.dayRow(today)
    if (day.admitted >= GLOBAL_DAILY_ANALYSES) {
      return deny('daily_capacity_reached', 'global', 'global', midnight, now)
    }
    if (heavy && day.heavy >= HEAVY_DAILY_ANALYSES) {
      return deny('daily_capacity_reached', 'daily', 'heavy', midnight, now)
    }

    const state = this.loadState()
    const estimate = EGRESS_ESTIMATE_BYTES[costClass]
    const denial = this.computeDenial(state, now, request.prefix, estimate, { day, prefixUse })
    if (denial !== null) return denial

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
      `INSERT INTO prefixes (day, prefix, count, attempts) VALUES (?, ?, 1, 1)
       ON CONFLICT (day, prefix) DO UPDATE SET count = count + 1, attempts = attempts + 1`,
      today,
      request.prefix,
    )
    state.lastAdmitAt = now
    if (state.purgedDay !== today) this.purge(now, state)
    await this.scheduleAlarm(state, now)
    return { ok: true, leaseId }
  }

  /**
   * Admit one metadata GET to the origin (the static snapshot could not
   * answer it). Each costs Render outbound bandwidth, so it is counted. A
   * warm API answers it from memory, but the first data request on a freshly
   * started process (after every Render spin-down) reads the API's data
   * snapshot from Neon and can wake it: so a lookup also passes the compute
   * and egress checks (4-7) and holds a lease, which the Worker settles with
   * the API's `Server-Timing` like an analysis's. Its counts are never
   * refunded.
   */
  async admitLookup(request: LookupRequest): Promise<LookupResult> {
    const now = Date.now()
    const today = utcDay(now)
    const midnight = nextUtcMidnight(now)
    // As for analyses, a count denial reads one or two rows and writes none.
    const prefixUse = this.prefixUse(today, request.prefix)
    if (prefixUse.lookups >= PREFIX_DAILY_LOOKUPS) {
      return deny('rate_limited', 'client', 'metadata', midnight, now)
    }
    const day = this.dayRow(today)
    if (day.lookups >= GLOBAL_DAILY_LOOKUPS) {
      return deny('daily_capacity_reached', 'global', 'metadata', midnight, now)
    }
    const state = this.loadState()
    const estimate = SNAPSHOT_READ_EGRESS_BYTES
    const denial = this.computeDenial(state, now, request.prefix, estimate, { day, prefixUse })
    if (denial !== null) return denial

    const leaseId = crypto.randomUUID()
    this.sql.exec(
      `INSERT INTO leases (id, day, prefix, heavy, admitted_at, egress_bytes, lookup)
       VALUES (?, ?, ?, 0, ?, ?, 1)`,
      leaseId,
      today,
      request.prefix,
      now,
      SNAPSHOT_READ_EGRESS_BYTES,
    )
    this.sql.exec(
      `INSERT INTO days (day, lookups) VALUES (?, 1)
       ON CONFLICT (day) DO UPDATE SET lookups = lookups + 1`,
      today,
    )
    this.sql.exec(
      `INSERT INTO prefixes (day, prefix, count, lookups) VALUES (?, ?, 0, 1)
       ON CONFLICT (day, prefix) DO UPDATE SET lookups = lookups + 1`,
      today,
      request.prefix,
    )
    if (state.purgedDay !== today) this.purge(now, state)
    await this.scheduleAlarm(state, now)
    return { ok: true, leaseId }
  }

  async settle(leaseId: string, report: SettleReport): Promise<void> {
    const lease = this.sql
      .exec<LeaseRow>('SELECT * FROM leases WHERE id = ?', leaseId)
      .toArray()[0]
    // Unknown or already expired (and charged): nothing to do.
    if (lease === undefined) return
    const now = Date.now()
    this.sql.exec('DELETE FROM leases WHERE id = ?', leaseId)

    const { kind } = report
    // A lookup's counts are never refunded; its compute is charged as an analysis's.
    const analysis = lease.lookup !== 1
    // A refusal or reuse that ran statements keeps its counts (see `SettleKind`).
    const ranStatements = report.dbMs !== null && report.dbMs > 0
    const refundCounts =
      analysis &&
      (kind === 'not_reached' ||
        kind === 'shed' ||
        ((kind === 'rejected' || kind === 'reused') && !ranStatements))
    const refundPrefix = analysis && (kind === 'not_reached' || kind === 'shed')
    if (refundCounts) {
      this.sql.exec(
        `UPDATE days SET admitted = MAX(0, admitted - 1), heavy = MAX(0, heavy - ?),
         refunded = refunded + 1 WHERE day = ?`,
        lease.heavy,
        lease.day,
      )
    }
    let cuS = 0
    const dbTouched = kind !== 'not_reached' && report.dbMs !== 0
    if (dbTouched) {
      const state = this.loadState()
      const start = dbStart(lease.admitted_at, now, report.dbMs)
      const settled = chargeWindow(state.charged, start, now, now - LEASE_TTL_MS)
      cuS = cuSeconds(settled.addedMs)
      this.charge(state, lease.day, cuS, lease.egress_bytes)
      state.charged = settled.charged
      this.saveState(state)
    }
    if (refundPrefix || cuS > 0) this.chargePrefix(lease, cuS, refundPrefix)
  }

  async budget(): Promise<BudgetReport> {
    const now = Date.now()
    const today = utcDay(now)
    const state = this.loadState()
    const day = this.dayRow(today)
    const leases = this.leaseRows()
    const prefixes = this.sql
      .exec<{
        active: number
        max_admitted: number | null
        max_attempts: number | null
        max_cu_s: number | null
        max_lookups: number | null
      }>(
        `SELECT count(*) AS active, max(count) AS max_admitted, max(attempts) AS max_attempts,
           max(cu_s) AS max_cu_s, max(lookups) AS max_lookups
         FROM prefixes WHERE day = ?`,
        today,
      )
      .one()
    const rolling = this.sql
      .exec<{ cu_s: number | null }>(
        'SELECT sum(cu_s) AS cu_s FROM days WHERE day > ?',
        utcDay(now - 31 * DAY_MS),
      )
      .one()

    const todaysLeases = leases.filter((lease) => lease.day === today)
    const provisionalTodayS = cuSeconds(
      uncoveredLength(todaysLeases.map(leaseInterval), state.charged),
    )
    const ceiling = dailyCeilingCuHours(state, now, this.neonConfigured())
    const prefixCuLimitS = PREFIX_DAILY_CU_SHARE * ceiling * 3600
    const neon = currentNeon(state, now, this.neonConfigured())
    const egressCeiling = neon === null ? EGRESS_DAILY_CONSERVATIVE_BYTES : null
    const provisionalEgress = sumEgress(todaysLeases)

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
      const provisionalS = cuSeconds(uncoveredLength(leases.map(leaseInterval), state.charged))
      const bridgeCuHours = (state.cuTotalS - neon.cuBaseS + provisionalS) / 3600
      const bridgeEgress = state.egressTotal - neon.egressBase + sumEgress(leases)
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
        in_flight: leases.filter((lease) => lease.lookup !== 1).length,
        refunded: day.refunded,
      },
      prefixes: {
        active: prefixes.active,
        max_admitted: prefixes.max_admitted ?? 0,
        limit: PREFIX_DAILY_ANALYSES,
        max_attempts: prefixes.max_attempts ?? 0,
        attempts_limit: PREFIX_DAILY_ADMISSIONS,
        max_cu_hours: round((prefixes.max_cu_s ?? 0) / 3600, 4),
        cu_limit_cu_hours: round(prefixCuLimitS / 3600, 4),
      },
      compute_today: {
        settled_cu_hours: round(day.cu_s / 3600, 4),
        provisional_cu_hours: round(provisionalTodayS / 3600, 4),
        ceiling_cu_hours: round(ceiling, 4),
      },
      egress_today: {
        settled_bytes: Math.round(day.egress_bytes),
        provisional_bytes: Math.round(provisionalEgress),
        ceiling_bytes: egressCeiling === null ? null : Math.round(egressCeiling),
      },
      metadata: {
        origin_calls: day.lookups,
        limit: GLOBAL_DAILY_LOOKUPS,
        max_prefix: prefixes.max_lookups ?? 0,
        prefix_limit: PREFIX_DAILY_LOOKUPS,
      },
      compute_rolling_31d_cu_hours: round((rolling.cu_s ?? 0) / 3600, 4),
      month,
      utilization: {
        prefix: round(
          Math.max(
            (prefixes.max_admitted ?? 0) / PREFIX_DAILY_ANALYSES,
            (prefixes.max_attempts ?? 0) / PREFIX_DAILY_ADMISSIONS,
            (prefixes.max_cu_s ?? 0) / prefixCuLimitS,
          ),
          4,
        ),
        global: round(day.admitted / GLOBAL_DAILY_ANALYSES, 4),
        heavy: round(day.heavy / HEAVY_DAILY_ANALYSES, 4),
        cu_day: round(dayCuHours / ceiling, 4),
        egress_day:
          egressCeiling === null
            ? null
            : round((day.egress_bytes + provisionalEgress) / egressCeiling, 4),
        cu_month: monthUse === null ? null : round(monthUse.cu / monthUse.allowance, 4),
        egress: monthUse === null ? null : round(monthUse.egress / EGRESS_STOP_BYTES, 4),
        metadata: round(
          Math.max(
            day.lookups / GLOBAL_DAILY_LOOKUPS,
            (prefixes.max_lookups ?? 0) / PREFIX_DAILY_LOOKUPS,
          ),
          4,
        ),
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

  /**
   * Checks 4-7 (see the module comment) for a candidate that would start a
   * provisional compute window now and is estimated at `egressBytes` of Neon
   * egress, after expiring stale leases (and saving `state` if any did): the
   * denial, or null when every check passes. `seen` holds the day and prefix
   * rows the caller read for its count checks, re-read only after an expiry.
   */
  private computeDenial(
    state: GovernorState,
    now: number,
    prefix: string,
    egressBytes: number,
    seen: { day: DayRow; prefixUse: PrefixRow },
  ): Denied | null {
    const today = utcDay(now)
    const midnight = nextUtcMidnight(now)
    let { day, prefixUse } = seen
    if (this.expireLeases(now, state)) {
      this.saveState(state)
      day = this.dayRow(today)
      prefixUse = this.prefixUse(today, prefix)
    }
    const leases = this.leaseRows()
    const todays = leases.filter((lease) => lease.day === today)
    const candidate = provisionalInterval(now)
    const ceilingS = dailyCeilingCuHours(state, now, this.neonConfigured()) * 3600

    // The network's own share of the day's compute: its settled charges, plus
    // what its in-flight leases and this one would add. Counting analyses
    // alone let one network that spaced cheap ones (~610 CU-s each, no shared
    // wake session) spend the whole ceiling, and lock everyone else out.
    const own = todays.filter((lease) => lease.prefix === prefix).map(leaseInterval)
    const prefixCuS =
      prefixUse.cu_s + cuSeconds(uncoveredLength([...own, candidate], state.charged))
    if (prefixCuS > PREFIX_DAILY_CU_SHARE * ceilingS) {
      return deny('rate_limited', 'client', 'prefix', midnight, now)
    }

    const dayCuS =
      day.cu_s +
      cuSeconds(uncoveredLength([...todays.map(leaseInterval), candidate], state.charged))
    if (dayCuS > ceilingS) {
      return deny('daily_capacity_reached', 'daily', 'cu_day', midnight, now)
    }

    const neon = currentNeon(state, now, this.neonConfigured())
    if (neon === null) {
      // No monthly figure to check egress against: hold each day to 1/31 of the stop.
      const dayEgress = day.egress_bytes + sumEgress(todays) + egressBytes
      if (dayEgress > EGRESS_DAILY_CONSERVATIVE_BYTES) {
        return deny('daily_capacity_reached', 'daily', 'egress_day', midnight, now)
      }
      return null
    }
    const egress =
      neon.dataTransferBytes +
      (state.egressTotal - neon.egressBase) +
      sumEgress(leases) +
      egressBytes
    if (egress >= EGRESS_STOP_BYTES) {
      return deny('capacity_paused', 'monthly', 'egress', neon.periodEnd, now)
    }
    const provisionalS = cuSeconds(
      uncoveredLength([...leases.map(leaseInterval), candidate], state.charged),
    )
    const monthCuHours =
      (neon.computeTimeSeconds + (state.cuTotalS - neon.cuBaseS) + provisionalS) / 3600
    const allowance = pacingAllowance(elapsedShare(now, neon.periodStart, neon.periodEnd))
    if (monthCuHours > allowance) {
      const resumesAt = pacingResumesAt(monthCuHours, neon.periodStart, neon.periodEnd)
      return deny('capacity_paused', 'monthly', 'cu_month', resumesAt, now)
    }
    return null
  }

  // --- storage -----------------------------------------------------------

  private loadState(): GovernorState {
    const row = this.sql
      .exec<{ value: string }>('SELECT value FROM state WHERE id = 1')
      .toArray()[0]
    if (row === undefined) return { ...INITIAL_STATE }
    let stored: unknown
    try {
      stored = JSON.parse(row.value)
    } catch {
      stored = null
    }
    return sanitizeState(stored, Date.now())
  }

  private saveState(state: GovernorState): void {
    this.sql.exec(
      `INSERT INTO state (id, value) VALUES (1, ?)
       ON CONFLICT (id) DO UPDATE SET value = excluded.value`,
      JSON.stringify(state),
    )
  }

  /** A prefix's use of its budgets on `day` (see `PrefixRow`; `spent` for impossible values). */
  private prefixUse(day: string, prefix: string): PrefixRow {
    const row = this.sql
      .exec<PrefixRow>(
        'SELECT count, attempts, cu_s, lookups FROM prefixes WHERE day = ? AND prefix = ?',
        day,
        prefix,
      )
      .toArray()[0]
    if (row === undefined) return { count: 0, attempts: 0, cu_s: 0, lookups: 0 }
    return {
      count: spent(row.count),
      attempts: spent(row.attempts),
      cu_s: spent(row.cu_s),
      lookups: spent(row.lookups),
    }
  }

  /** `day`'s totals (`spent` for impossible values). */
  private dayRow(day: string): DayRow {
    const row = this.sql
      .exec<DayRow & { day: string }>('SELECT * FROM days WHERE day = ?', day)
      .toArray()[0]
    if (row === undefined) {
      return { admitted: 0, heavy: 0, cu_s: 0, egress_bytes: 0, refunded: 0, lookups: 0 }
    }
    return {
      admitted: spent(row.admitted),
      heavy: spent(row.heavy),
      cu_s: spent(row.cu_s),
      egress_bytes: spent(row.egress_bytes),
      refunded: spent(row.refunded),
      lookups: spent(row.lookups),
    }
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
   * Add a settled or expired lease's compute to its prefix, and give back its
   * analysis count when `refund`. A prefix row deleted at midnight stays deleted.
   */
  private chargePrefix(lease: LeaseRow, cuS: number, refund: boolean): void {
    this.sql.exec(
      'UPDATE prefixes SET count = MAX(0, count - ?), cu_s = cu_s + ? WHERE day = ? AND prefix = ?',
      refund ? 1 : 0,
      cuS,
      lease.day,
      lease.prefix,
    )
  }

  /**
   * Charge, and drop, every lease older than `LEASE_TTL_MS`: its settle was
   * lost (the Worker invocation died, or was cancelled when its client left).
   * Each is charged like a settle at the end of the API's deadline, as part
   * of the union: leases admitted together share one window, as they would
   * have had they settled, however late the next event comes. Counts are
   * kept. Returns whether any expired.
   */
  private expireLeases(now: number, state: GovernorState): boolean {
    const stale = this.sql
      .exec<LeaseRow>(
        'SELECT * FROM leases WHERE admitted_at < ? ORDER BY admitted_at',
        now - LEASE_TTL_MS,
      )
      .toArray()
    if (stale.length === 0) return false
    // Keep the windows charged here until every stale lease is in (the oldest
    // one's admission is the horizon), then trim to what a live lease can reach.
    const horizon = stale[0]!.admitted_at
    for (const lease of stale) {
      const expired = chargeWindow(
        state.charged,
        lease.admitted_at,
        lease.admitted_at + PROVISIONAL_BUSY_MS,
        horizon,
      )
      const cuS = cuSeconds(expired.addedMs)
      this.charge(state, lease.day, cuS, lease.egress_bytes)
      state.charged = expired.charged
      if (cuS > 0) this.chargePrefix(lease, cuS, false)
      this.sql.exec('DELETE FROM leases WHERE id = ?', lease.id)
    }
    state.charged = state.charged.filter(([, end]) => end > now - LEASE_TTL_MS)
    return true
  }

  /** Delete earlier days' prefix rows (their key has rotated) and old day rows. */
  private purge(now: number, state: GovernorState): void {
    const today = utcDay(now)
    this.sql.exec('DELETE FROM prefixes WHERE day < ?', today)
    this.sql.exec('DELETE FROM days WHERE day < ?', utcDay(now - DAY_RETENTION_DAYS * DAY_MS))
    state.purgedDay = today
  }

  /**
   * Delete rows this code never writes, so only a version deployed with a
   * leaked token could have: day and prefix rows dated after today (once its
   * day came, each would refuse that whole day before any purge ran), and
   * leases admitted in the future, dated other than their admission, or
   * holding more egress than any estimate (they would never expire, or would
   * charge another day). Runs when the instance starts: every deploy
   * restarts it. The `day` conditions read only the rows they delete.
   */
  private dropImpossibleRows(now: number): void {
    const today = utcDay(now)
    this.sql.exec('DELETE FROM days WHERE day > ?', today)
    this.sql.exec('DELETE FROM prefixes WHERE day > ?', today)
    // `IS NOT 1`, not `NOT`: a condition that reads NULL (`strftime` gives no
    // date for an admission outside SQLite's date range) deletes the row too.
    this.sql.exec(
      `DELETE FROM leases WHERE (
         admitted_at <= ? AND day = strftime('%Y-%m-%d', admitted_at / 1000, 'unixepoch')
         AND egress_bytes >= 0 AND egress_bytes <= ? AND heavy IN (0, 1) AND lookup IN (0, 1)
       ) IS NOT 1`,
      now,
      MAX_LEASE_EGRESS_BYTES,
    )
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
      const neon = sanitizeNeon(
        {
          ...result,
          polledAt: now,
          cuBaseS: previous?.cuAtPollS ?? Math.max(0, state.cuTotalS - today.cu_s),
          egressBase:
            previous?.egressAtPoll ?? Math.max(0, state.egressTotal - today.egress_bytes),
          cuAtPollS: state.cuTotalS,
          egressAtPoll: state.egressTotal,
        },
        Date.now(),
        state,
      )
      // What the next load would drop (a period over 32 days) is not kept.
      state.pollError = neon === null ? 'invalid_response' : null
      if (neon !== null) state.neon = neon
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

/** The egress estimates `leases` hold. */
function sumEgress(leases: readonly LeaseRow[]): number {
  return leases.reduce((sum, lease) => sum + lease.egress_bytes, 0)
}

function round(value: number, digits: number): number {
  const factor = 10 ** digits
  return Math.round(value * factor) / factor
}

/** A finite, non-negative number, as every count, charge and time this code stores. */
function isAmount(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

/**
 * A stored count or charge, or `Infinity` when it is not a finite,
 * non-negative number: this code never writes one, and read as a number a
 * negative or text value would lift the budget it counts for the rest of the
 * day. As spent, it refuses that budget instead, as a planted value at the
 * limit would anyway.
 */
function spent(value: unknown): number {
  return isAmount(value) ? value : Number.POSITIVE_INFINITY
}

/**
 * The stored state, with what this code could not have written as of `now`
 * reset to its initial value:
 *
 * - charged windows ending more than `MAX_WINDOW_AHEAD_MS` after now (one
 *   to the end of time made every later analysis free of compute charges);
 * - counters and times that are not finite and non-negative, and times after now;
 * - Neon ground truth that no poll could have produced (`sanitizeNeon`).
 */
function sanitizeState(stored: unknown, now: number): GovernorState {
  const raw = (typeof stored === 'object' && stored !== null ? stored : {}) as Record<
    string,
    unknown
  >
  const past = (value: unknown): number => (isAmount(value) && value <= now ? value : 0)
  const charged = Array.isArray(raw.charged)
    ? mergeIntervals(
        raw.charged.filter(
          (window: unknown): window is Interval =>
            Array.isArray(window) &&
            window.length === 2 &&
            isAmount(window[0]) &&
            isAmount(window[1]) &&
            window[1] <= now + MAX_WINDOW_AHEAD_MS,
        ),
      )
    : []
  const purgedDay = typeof raw.purgedDay === 'string' ? raw.purgedDay : ''
  const state: GovernorState = {
    charged,
    cuTotalS: isAmount(raw.cuTotalS) ? raw.cuTotalS : 0,
    egressTotal: isAmount(raw.egressTotal) ? raw.egressTotal : 0,
    lastAdmitAt: past(raw.lastAdmitAt),
    lastPollAt: past(raw.lastPollAt),
    pollError: typeof raw.pollError === 'string' ? raw.pollError.slice(0, 64) : null,
    purgedDay:
      /^\d{4}-\d{2}-\d{2}$/.test(purgedDay) && purgedDay <= utcDay(now) ? purgedDay : '',
    alarmAt: isAmount(raw.alarmAt) ? raw.alarmAt : null,
    neon: null,
  }
  state.neon = sanitizeNeon(raw.neon, now, state)
  return state
}

/** The fields of `NeonUsage`, each a finite, non-negative number. */
const NEON_FIELDS: readonly (keyof NeonUsage)[] = [
  'polledAt',
  'computeTimeSeconds',
  'dataTransferBytes',
  'periodStart',
  'periodEnd',
  'cuBaseS',
  'egressBase',
  'cuAtPollS',
  'egressAtPoll',
]

/**
 * `stored` as ground truth, or null when no poll could have produced it: a
 * field that is not a finite, non-negative number; a poll after `now`; a
 * consumption period that is empty, longer than `NEON_MAX_PERIOD_MS` or ends
 * more than that after the poll; or bridge bases above the bridge counters
 * (`totals`), which would subtract charges instead of adding them.
 */
function sanitizeNeon(
  stored: unknown,
  now: number,
  totals: Pick<GovernorState, 'cuTotalS' | 'egressTotal'>,
): NeonUsage | null {
  if (typeof stored !== 'object' || stored === null) return null
  const raw = stored as Partial<Record<keyof NeonUsage, unknown>>
  if (!NEON_FIELDS.every((field) => isAmount(raw[field]))) return null
  const neon = Object.fromEntries(
    NEON_FIELDS.map((field) => [field, raw[field]]),
  ) as unknown as NeonUsage
  const possible =
    neon.polledAt <= now &&
    neon.periodStart < neon.periodEnd &&
    neon.periodEnd - neon.periodStart <= NEON_MAX_PERIOD_MS &&
    neon.periodEnd - neon.polledAt <= NEON_MAX_PERIOD_MS &&
    neon.cuBaseS <= neon.cuAtPollS &&
    neon.cuAtPollS <= totals.cuTotalS &&
    neon.egressBase <= neon.egressAtPoll &&
    neon.egressAtPoll <= totals.egressTotal
  return possible ? neon : null
}

/**
 * Ground truth for the current consumption period, however old; none while
 * no Neon key is configured (no poll can have written it then).
 */
function currentNeon(state: GovernorState, now: number, configured: boolean): NeonUsage | null {
  return configured && state.neon !== null && now < state.neon.periodEnd ? state.neon : null
}

/** Ground truth recent enough to earn the larger daily ceiling. */
function groundTruthFresh(state: GovernorState, now: number, configured: boolean): boolean {
  const neon = currentNeon(state, now, configured)
  return neon !== null && now - neon.polledAt <= GROUND_TRUTH_MAX_AGE_MS
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
