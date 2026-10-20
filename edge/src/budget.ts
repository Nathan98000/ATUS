/**
 * Pure arithmetic of the quota governor (ADR-015 "Governor budgets"): UTC
 * days, the Neon compute charge model and the month-to-date pacing line.
 * No storage and no clock of its own, so every rule is unit-tested directly.
 *
 * Compute charge model (the conservative bridge of ADR-011). Neon bills a
 * compute from the moment it wakes until it suspends, 5 minutes after the
 * last query. The governor assumes the worst autoscaler behaviour: the
 * compute sits at its maximum (2 CU) for that whole time. An analysis whose
 * DB work spans [s, e] keeps the compute up over [s, e + 300 s], so the
 * month's charge is 2 CU x the length of the union of those intervals. The
 * governor keeps the windows it has already charged, so an analysis costs
 * only the time it adds outside them, in whatever order analyses settle.
 */

import {
  DB_START_MARGIN_MS,
  NEON_IDLE_TAIL_MS,
  NEON_MAX_CU,
  PACING_CAP_CU_HOURS,
  PACING_FLOOR_CU_HOURS,
  PACING_SLOPE_CU_HOURS,
  PROVISIONAL_BUSY_MS,
} from './constants'

export const DAY_MS = 86_400_000

/** `YYYY-MM-DD` of the UTC day containing `ms`. */
export function utcDay(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10)
}

/** Epoch ms of the next 00:00 UTC strictly after `ms`. */
export function nextUtcMidnight(ms: number): number {
  return (Math.floor(ms / DAY_MS) + 1) * DAY_MS
}

/** CU-seconds charged for `ms` of compute time at the maximum size. */
export function cuSeconds(ms: number): number {
  return (NEON_MAX_CU * ms) / 1000
}

/** A half-open time interval in epoch ms. */
export type Interval = readonly [start: number, end: number]

/** The interval an admitted, unsettled analysis is assumed to keep the compute up. */
export function provisionalInterval(admittedAt: number): Interval {
  return [admittedAt, admittedAt + PROVISIONAL_BUSY_MS + NEON_IDLE_TAIL_MS]
}

/** `intervals` as sorted, disjoint intervals: overlapping or touching ones merged, empty ones dropped. */
export function mergeIntervals(intervals: readonly Interval[]): Interval[] {
  const sorted = intervals.filter(([start, end]) => end > start).sort((a, b) => a[0] - b[0])
  const merged: [number, number][] = []
  for (const [start, end] of sorted) {
    const last = merged[merged.length - 1]
    if (last !== undefined && start <= last[1]) last[1] = Math.max(last[1], end)
    else merged.push([start, end])
  }
  return merged
}

/**
 * Length in ms of the union of `intervals`, counting only time outside
 * `charged` (the windows settled analyses have already charged).
 */
export function uncoveredLength(
  intervals: readonly Interval[],
  charged: readonly Interval[],
): number {
  const covered = mergeIntervals(charged)
  let total = 0
  for (const [start, end] of mergeIntervals(intervals)) {
    total += end - start
    for (const [from, to] of covered) {
      total -= Math.max(0, Math.min(end, to) - Math.max(start, from))
    }
  }
  return total
}

/**
 * When the DB work of a settled analysis began: `dbMs` (the API's
 * `Server-Timing: db;dur=`) before it ended, less a margin for the connect
 * and a Neon wake; never before it was admitted. Without `dbMs` the whole
 * time since admission counts (it then includes a Render spin-up: an
 * over-charge, never an under-charge).
 */
export function dbStart(admittedAt: number, settledAt: number, dbMs: number | null): number {
  if (dbMs === null) return admittedAt
  return Math.max(admittedAt, settledAt - dbMs - DB_START_MARGIN_MS)
}

/**
 * The charged windows with `window` added, merged, keeping only those that
 * end after `keepAfter`. Nothing charged later can reach back before the
 * admission of the oldest live lease, so the caller passes that horizon;
 * dropping a window too early could only over-charge.
 */
export function addCharged(
  charged: readonly Interval[],
  window: Interval,
  keepAfter: number,
): Interval[] {
  return mergeIntervals([...charged, window]).filter(([, end]) => end > keepAfter)
}

/**
 * Settle one analysis whose DB work spans [start, end]: the compute is up
 * over [start, end + tail]. Returns the milliseconds of that window not
 * charged yet, and the charged windows with it added (see `addCharged`).
 */
export function chargeWindow(
  charged: readonly Interval[],
  start: number,
  end: number,
  keepAfter: number,
): { charged: Interval[]; addedMs: number } {
  const window: Interval = [start, end + NEON_IDLE_TAIL_MS]
  return {
    addedMs: uncoveredLength([window], charged),
    charged: addCharged(charged, window, keepAfter),
  }
}

/** Share of the consumption period elapsed at `now`, in [0, 1]. */
export function elapsedShare(now: number, periodStart: number, periodEnd: number): number {
  if (!(periodEnd > periodStart)) return 1
  return Math.min(1, Math.max(0, (now - periodStart) / (periodEnd - periodStart)))
}

/** Month-to-date CU-hours allowed at `share` of the period: min(85, 10 + 75 x share). */
export function pacingAllowance(share: number): number {
  return Math.min(PACING_CAP_CU_HOURS, PACING_FLOOR_CU_HOURS + PACING_SLOPE_CU_HOURS * share)
}

/**
 * The earliest time the pacing line allows `neededCuHours` month to date:
 * the period end when the need is above the hard cap.
 */
export function pacingResumesAt(
  neededCuHours: number,
  periodStart: number,
  periodEnd: number,
): number {
  if (neededCuHours > PACING_CAP_CU_HOURS) return periodEnd
  const share = Math.max(0, (neededCuHours - PACING_FLOOR_CU_HOURS) / PACING_SLOPE_CU_HOURS)
  return Math.min(periodEnd, periodStart + Math.ceil(share * (periodEnd - periodStart)))
}

/** Whole seconds from `now` until `at`, at least 1 (a `Retry-After` value). */
export function secondsUntil(at: number, now: number): number {
  return Math.max(1, Math.ceil((at - now) / 1000))
}
