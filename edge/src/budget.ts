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
 * month's charge is 2 CU x the length of the union of those intervals. An
 * analysis inside an already-charged window costs only the extension it adds.
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

/**
 * Length in ms of the union of `intervals`, counting only time after
 * `chargedUntil` (the end of the window already charged by settled analyses).
 */
export function unionLengthAfter(intervals: readonly Interval[], chargedUntil: number): number {
  const clipped = intervals
    .map(([start, end]): Interval => [Math.max(start, chargedUntil), end])
    .filter(([start, end]) => end > start)
    .sort((a, b) => a[0] - b[0])
  let total = 0
  let current: [number, number] | null = null
  for (const [start, end] of clipped) {
    if (current !== null && start <= current[1]) {
      current[1] = Math.max(current[1], end)
      continue
    }
    if (current !== null) total += current[1] - current[0]
    current = [start, end]
  }
  if (current !== null) total += current[1] - current[0]
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
 * Settle one analysis into the charged window. Returns the new window end
 * and the milliseconds of compute time it adds: the part of
 * [start, end + tail] beyond the window already charged.
 */
export function extendWindow(
  chargedUntil: number,
  start: number,
  end: number,
): { chargedUntil: number; addedMs: number } {
  const until = end + NEON_IDLE_TAIL_MS
  const addedMs = Math.max(0, until - Math.max(start, chargedUntil))
  return { chargedUntil: Math.max(chargedUntil, until), addedMs }
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
