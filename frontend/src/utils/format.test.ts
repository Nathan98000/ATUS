import { describe, expect, it } from 'vitest'

import {
  formatAxisTicks,
  formatClockTick,
  formatConfidenceInterval,
  formatDifferenceInterval,
  formatDifferenceValue,
  formatConfidenceLevel,
  formatDurationTick,
  formatDurationWords,
  formatEstimate,
  formatLikelyRange,
  formatMinutesAsDuration,
  formatMinutesDetailed,
  formatPeople,
  formatProportionAsPercent,
  formatStandardError,
  formatValueShort,
  formatYears,
  unitLabel,
} from './format'

describe('duration formatting', () => {
  it('renders hours and minutes rounded to the nearest minute', () => {
    expect(formatMinutesAsDuration(528.75)).toBe('8h 49m')
    expect(formatMinutesAsDuration(541.96)).toBe('9h 2m')
    expect(formatMinutesAsDuration(60)).toBe('1h 0m')
  })

  it('omits hours below one hour', () => {
    expect(formatMinutesAsDuration(42.4)).toBe('42m')
    expect(formatMinutesAsDuration(0)).toBe('0m')
  })

  it('keeps one decimal in the detailed form', () => {
    expect(formatMinutesDetailed(528.7543524721476)).toBe('528.8')
    expect(formatMinutesDetailed(112)).toBe('112.0')
  })
})

describe('percentages and people', () => {
  it('formats proportions as one-decimal percentages', () => {
    expect(formatProportionAsPercent(0.7277947880733225)).toBe('72.8%')
    expect(formatProportionAsPercent(1)).toBe('100.0%')
  })

  it('formats large populations in millions and small counts with separators', () => {
    expect(formatPeople(272912483.1)).toBe('272.9 million')
    expect(formatPeople(41200)).toBe('41,200')
  })
})

describe('estimate formatting by unit', () => {
  it('uses duration for minutes per day', () => {
    const formatted = formatEstimate(528.75, 'minutes_per_day')
    expect(formatted.primary).toBe('8h 49m')
    expect(formatted.secondary).toBe('528.8 minutes per day')
  })

  it('uses percent for participation', () => {
    expect(formatEstimate(0.7278, 'proportion_of_population').primary).toBe('72.8%')
  })

  it('uses people for persons per day', () => {
    expect(formatEstimate(198654321, 'persons_per_day').primary).toBe('198.7 million')
  })

  it('falls back to a plain number for unknown future units', () => {
    const formatted = formatEstimate(12.34, 'hours_per_week')
    expect(formatted.primary).toBe('12.3')
    expect(formatted.secondary).toBe('hours per week')
  })
})

describe('uncertainty formatting', () => {
  it('renders SEs in the estimate scale, percentage points for proportions', () => {
    expect(formatStandardError(2.938, 'minutes_per_day')).toBe('2.9 min')
    expect(formatStandardError(0.00728, 'proportion_of_population')).toBe('0.7 pp')
  })

  it('renders confidence intervals with matching precision', () => {
    expect(formatConfidenceInterval(523.0, 534.51, 'minutes_per_day')).toBe(
      '523.0 to 534.5 min',
    )
    expect(formatConfidenceInterval(0.7135, 0.742, 'proportion_of_population')).toBe(
      '71.4% to 74.2%',
    )
  })

  it('formats confidence levels as whole percents', () => {
    expect(formatConfidenceLevel(0.95)).toBe('95%')
    expect(formatConfidenceLevel(0.9)).toBe('90%')
  })

  // Review round 3: the API accepts any level in (0.5, 1); rounding to a whole
  // percent named a different level than the one computed (and exported).
  it('never rounds a confidence level to a different one', () => {
    expect(formatConfidenceLevel(0.975)).toBe('97.5%')
    expect(formatConfidenceLevel(0.9545)).toBe('95.45%')
    expect(formatConfidenceLevel(0.995)).toBe('99.5%')
    expect(formatConfidenceLevel(0.999)).toBe('99.9%')
    expect(formatConfidenceLevel(0.99999)).toBe('99.999%')
    expect(formatConfidenceLevel(0.99)).toBe('99%')
    // Binary × 100 noise never shows (0.57 × 100 is 56.99999999999999)…
    expect(formatConfidenceLevel(0.57)).toBe('57%')
    // …and no level the API accepts collapses onto 100% or 50%: the API
    // computes 0.9999999999999996 (z ≈ 8.13), and these two are as exported.
    expect(formatConfidenceLevel(0.9999999999999996)).toBe('99.99999999999996%')
    expect(formatConfidenceLevel(0.5000000000000001)).toBe('50.00000000000001%')
  })
})

describe('years and units', () => {
  it('collapses contiguous year lists into ranges', () => {
    expect(formatYears([2025])).toBe('2025')
    expect(formatYears([2003, 2004, 2005])).toBe('2003–2005')
    expect(formatYears([2021, 2019])).toBe('2019, 2021')
  })

  it('labels every known unit and humanizes unknown ones', () => {
    expect(unitLabel('minutes_per_day')).toBe('minutes per day')
    expect(unitLabel('persons_per_day')).toBe('people per day')
    expect(unitLabel('future_unit')).toBe('future unit')
  })

  it('never renders a value without context', () => {
    expect(formatValueShort(528.75, 'minutes_per_day')).toContain('min')
    expect(formatValueShort(0.5, 'proportion_of_population')).toContain('%')
  })
})

describe('differences (review finding: pp, never %)', () => {
  it('formats proportion differences and their CIs in percentage points', () => {
    expect(formatDifferenceValue(-0.023, 'proportion_of_population')).toBe('-2.3 pp')
    expect(formatDifferenceInterval(-0.031, -0.015, 'proportion_of_population')).toBe(
      '-3.1 to -1.5 pp',
    )
  })

  it('keeps minute differences in the estimate scale', () => {
    expect(formatDifferenceValue(-11.09, 'minutes_per_day')).toBe('-11.1 min')
  })
})

describe('adaptive precision (review finding: no 0.0 floor)', () => {
  it('keeps small nonzero SEs and rates visible', () => {
    expect(formatStandardError(0.0004, 'proportion_of_population')).toBe('0.04 pp')
    expect(formatProportionAsPercent(0.0004)).toBe('0.04%')
    expect(formatProportionAsPercent(0)).toBe('0.0%')
  })
})

describe('axis tick labels (review finding: no duplicate labels)', () => {
  it('adds decimals until proportion labels are distinct', () => {
    expect(formatAxisTicks([0.715, 0.72, 0.725], 'proportion_of_population')).toEqual([
      '71.5%',
      '72.0%',
      '72.5%',
    ])
    expect(formatAxisTicks([0.7, 0.72, 0.74], 'proportion_of_population')).toEqual([
      '70%',
      '72%',
      '74%',
    ])
  })

  it('labels minute ticks plainly', () => {
    expect(formatAxisTicks([300, 320, 340], 'minutes_per_day')).toEqual(['300', '320', '340'])
  })
})

describe('durations in words and on axes', () => {
  it('writes durations for sentences', () => {
    expect(formatDurationWords(23.2)).toBe('23 minutes')
    expect(formatDurationWords(-11.1)).toBe('11 minutes')
    expect(formatDurationWords(1)).toBe('1 minute')
    expect(formatDurationWords(72)).toBe('1 hour 12 minutes')
    expect(formatDurationWords(120)).toBe('2 hours')
    expect(formatDurationWords(0.3)).toBe('under a minute')
    expect(formatDurationWords(0)).toBe('0 minutes')
  })

  it('labels whole-minute ticks like a clock', () => {
    expect([0, 30, 60, 90, 180].map(formatDurationTick)).toEqual([
      '0',
      '30m',
      '1h',
      '1h 30m',
      '3h',
    ])
  })
})

describe('readers’ forms of axis ticks and ranges', () => {
  it('pads minutes on a clock axis so labels line up', () => {
    const ticks = [300, 310, 320]
    expect(ticks.map((tick) => formatClockTick(tick, ticks))).toEqual([
      '5h 00m',
      '5h 10m',
      '5h 20m',
    ])
    expect([360, 420].map((tick) => formatClockTick(tick, [360, 420]))).toEqual(['6h', '7h'])
    expect([30, 45].map((tick) => formatClockTick(tick, [30, 45]))).toEqual(['30m', '45m'])
  })

  it('says a likely range as clock times, or in minutes when they would read the same', () => {
    expect(formatLikelyRange(522.995, 534.513, 'minutes_per_day')).toBe('8h 43m to 8h 55m')
    expect(formatLikelyRange(42.2, 42.4, 'minutes_per_day')).toBe('42.2 to 42.4 min')
    expect(formatLikelyRange(0.7135, 0.742, 'proportion_of_population')).toBe('71.4% to 74.2%')
  })
})
