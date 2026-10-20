/** The governor's pure arithmetic (src/budget.ts): days, the compute charge model, pacing. */

import { describe, expect, it } from 'vitest'
import {
  DAY_MS,
  addCharged,
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
  type Interval,
} from '../../src/budget'
import { DAILY_CU_HOURS_CONSERVATIVE } from '../../src/constants'

const T = Date.UTC(2026, 9, 20, 12, 0, 0)
const S = 1000
const MIN = 60 * S

describe('UTC days', () => {
  it('names the UTC day and finds the next midnight', () => {
    expect(utcDay(Date.UTC(2026, 9, 20, 23, 59, 59, 999))).toBe('2026-10-20')
    expect(utcDay(Date.UTC(2026, 9, 21))).toBe('2026-10-21')
    expect(nextUtcMidnight(T)).toBe(Date.UTC(2026, 9, 21))
    // Exactly at midnight, the next one is a day later.
    expect(nextUtcMidnight(Date.UTC(2026, 9, 21))).toBe(Date.UTC(2026, 9, 22))
  })

  it('rounds Retry-After up to whole seconds, at least 1', () => {
    expect(secondsUntil(T + 1500, T)).toBe(2)
    expect(secondsUntil(T, T)).toBe(1)
    expect(secondsUntil(T - 5000, T)).toBe(1)
  })
})

describe('compute charge model', () => {
  it('charges every second at 2 CU', () => {
    expect(cuSeconds(300 * S)).toBe(600)
  })

  it('assumes 90 s of DB time plus the 300 s tail for an unsettled analysis', () => {
    expect(provisionalInterval(T)).toEqual([T, T + 390 * S])
  })

  it('merges overlapping and touching intervals, drops empty ones', () => {
    expect(
      mergeIntervals([
        [T + 30 * S, T + 40 * S],
        [T, T + 10 * S],
        [T + 5 * S, T + 20 * S],
        [T + 40 * S, T + 45 * S],
        [T + 50 * S, T + 50 * S],
      ]),
    ).toEqual([
      [T, T + 20 * S],
      [T + 30 * S, T + 45 * S],
    ])
  })

  it('measures the union of overlapping and disjoint intervals', () => {
    expect(uncoveredLength([], [])).toBe(0)
    expect(
      uncoveredLength(
        [
          [T, T + 10 * S],
          [T + 5 * S, T + 20 * S],
          [T + 30 * S, T + 40 * S],
        ],
        [],
      ),
    ).toBe(30 * S)
  })

  it('counts only time outside the already-charged windows', () => {
    const intervals: Interval[] = [[T, T + 390 * S]]
    expect(uncoveredLength(intervals, [[T, T + 100 * S]])).toBe(290 * S)
    expect(uncoveredLength(intervals, [[T - 10 * S, T + 400 * S]])).toBe(0)
    // A charged window inside the interval, and one that began after it started.
    expect(uncoveredLength(intervals, [[T + 100 * S, T + 200 * S]])).toBe(290 * S)
    expect(
      uncoveredLength(intervals, [
        [T + 50 * S, T + 60 * S],
        [T + 380 * S, T + 500 * S],
      ]),
    ).toBe(370 * S)
  })

  it('makes concurrent analyses cost one window, not one each', () => {
    const ten = Array.from({ length: 10 }, (_, i) => provisionalInterval(T + i * S))
    expect(uncoveredLength(ten, [])).toBe(399 * S)
  })

  it('opens a new session at 2 CU x (DB time + 300 s tail)', () => {
    const { charged, addedMs } = chargeWindow([], T, T + 4 * S, 0)
    expect(addedMs).toBe(304 * S)
    expect(cuSeconds(addedMs)).toBe(608)
    expect(charged).toEqual([[T, T + 304 * S]])
  })

  it('charges an analysis inside the window only for the tail it adds', () => {
    const first = chargeWindow([], T, T + 4 * S, 0)
    const second = chargeWindow(first.charged, T + 60 * S, T + 62 * S, 0)
    expect(second.addedMs).toBe(58 * S)
    expect(second.charged).toEqual([[T, T + 362 * S]])
    // Entirely inside the window (it ended before the window's last DB work): nothing.
    expect(chargeWindow(second.charged, T + 1 * S, T + 2 * S, 0).addedMs).toBe(0)
  })

  it('starts a fresh session after the compute has suspended', () => {
    const first = chargeWindow([], T, T + 4 * S, 0)
    const later = chargeWindow(first.charged, T + 20 * MIN, T + 20 * MIN + S, 0)
    expect(later.addedMs).toBe(301 * S)
    expect(later.charged).toHaveLength(2)
  })

  it('charges the part before the window when an earlier-started analysis settles last', () => {
    // Settled first: DB time [T + 10 s, T + 12 s]. Then one whose DB time began at T.
    const short = chargeWindow([], T + 10 * S, T + 12 * S, 0)
    const long = chargeWindow(short.charged, T, T + 50 * S, 0)
    expect(short.addedMs + long.addedMs).toBe(350 * S)
    expect(long.charged).toEqual([[T, T + 350 * S]])
  })

  it('charges only the gap when a window spans two charged ones', () => {
    const charged: Interval[] = [
      [T, T + 302 * S],
      [T + 400 * S, T + 702 * S],
    ]
    const spanning = chargeWindow(charged, T, T + 402 * S, 0)
    expect(spanning.addedMs).toBe(98 * S)
    expect(spanning.charged).toEqual([[T, T + 702 * S]])
  })

  it('forgets charged windows that ended before the horizon (only ever an over-charge)', () => {
    const old: Interval = [T - 30 * MIN, T - 25 * MIN]
    expect(addCharged([old], [T, T + 300 * S], T - 10 * MIN)).toEqual([[T, T + 300 * S]])
    expect(addCharged([old], [T, T + 300 * S], T - 26 * MIN)).toEqual([old, [T, T + 300 * S]])
  })

  it('starts the DB time at Server-Timing db before the end (less a margin), never before admission', () => {
    expect(dbStart(T, T + 70 * S, 2000)).toBe(T + 63 * S)
    expect(dbStart(T, T + 3 * S, 2000)).toBe(T)
    expect(dbStart(T, T + 70 * S, null)).toBe(T)
  })

  it('keeps a whole day of fresh sessions under the conservative ceiling only up to ~17', () => {
    // 2.9 CU-h / (2 CU x 300 s) per session: the ADR's conservative bound.
    expect(Math.floor((DAILY_CU_HOURS_CONSERVATIVE * 3600) / cuSeconds(300 * S))).toBe(17)
  })
})

describe('month-to-date pacing', () => {
  const start = Date.UTC(2026, 9, 1)
  const end = Date.UTC(2026, 10, 1)

  it('measures the elapsed share of the consumption period', () => {
    expect(elapsedShare(start, start, end)).toBe(0)
    expect(elapsedShare(start + (end - start) / 2, start, end)).toBe(0.5)
    expect(elapsedShare(end + DAY_MS, start, end)).toBe(1)
    expect(elapsedShare(start - DAY_MS, start, end)).toBe(0)
    expect(elapsedShare(T, end, start)).toBe(1)
  })

  it('allows min(85, 10 + 75 x share)', () => {
    expect(pacingAllowance(0)).toBe(10)
    expect(pacingAllowance(0.5)).toBe(47.5)
    expect(pacingAllowance(1)).toBe(85)
  })

  it('finds when the allowance reaches a need, or the period end above the cap', () => {
    expect(pacingResumesAt(47.5, start, end)).toBe(start + (end - start) / 2)
    expect(pacingResumesAt(5, start, end)).toBe(start)
    expect(pacingResumesAt(85.1, start, end)).toBe(end)
    // The resume time really allows the need.
    const at = pacingResumesAt(30, start, end)
    expect(pacingAllowance(elapsedShare(at, start, end))).toBeGreaterThanOrEqual(30)
  })
})
