/** The governor's pure arithmetic (src/budget.ts): days, the compute charge model, pacing. */

import { describe, expect, it } from 'vitest'
import {
  DAY_MS,
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

  it('measures the union of overlapping and disjoint intervals', () => {
    expect(unionLengthAfter([], 0)).toBe(0)
    expect(
      unionLengthAfter(
        [
          [T, T + 10 * S],
          [T + 5 * S, T + 20 * S],
          [T + 30 * S, T + 40 * S],
        ],
        0,
      ),
    ).toBe(30 * S)
  })

  it('counts only time after the already-charged window', () => {
    const intervals: [number, number][] = [[T, T + 390 * S]]
    expect(unionLengthAfter(intervals, T + 100 * S)).toBe(290 * S)
    expect(unionLengthAfter(intervals, T + 400 * S)).toBe(0)
  })

  it('makes concurrent analyses cost one window, not one each', () => {
    const ten = Array.from({ length: 10 }, (_, i) => provisionalInterval(T + i * S))
    expect(unionLengthAfter(ten, 0)).toBe(399 * S)
  })

  it('opens a new session at 2 CU x (DB time + 300 s tail)', () => {
    const { chargedUntil, addedMs } = extendWindow(0, T, T + 4 * S)
    expect(addedMs).toBe(304 * S)
    expect(cuSeconds(addedMs)).toBe(608)
    expect(chargedUntil).toBe(T + 304 * S)
  })

  it('charges an analysis inside the window only for the tail it adds', () => {
    const first = extendWindow(0, T, T + 4 * S)
    const second = extendWindow(first.chargedUntil, T + 60 * S, T + 62 * S)
    expect(second.addedMs).toBe(58 * S)
    expect(second.chargedUntil).toBe(T + 362 * S)
    // Entirely inside the window (it ended before the window's last DB work): nothing.
    expect(extendWindow(second.chargedUntil, T + 1 * S, T + 2 * S).addedMs).toBe(0)
  })

  it('starts a fresh session after the compute has suspended', () => {
    const first = extendWindow(0, T, T + 4 * S)
    const later = extendWindow(first.chargedUntil, T + 20 * MIN, T + 20 * MIN + S)
    expect(later.addedMs).toBe(301 * S)
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
