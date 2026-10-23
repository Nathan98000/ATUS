/**
 * Presentation-only number formatting. These functions transform units for
 * display ("528.75" → "8h 49m"); they never change, round, or derive the
 * underlying data — raw API values stay untouched in state.
 *
 * Precision policy (documented in docs/frontend.md):
 * - durations: hours+minutes rounded to the nearest minute; detailed form
 *   shows one decimal of minutes;
 * - percentages: one decimal;
 * - standard errors and confidence bounds: same precision as the detailed
 *   estimate they qualify;
 * - people counts: millions with one decimal above 1M, thousands separators
 *   below;
 * - sample sizes: exact integers with thousands separators;
 * - confidence levels: every digit (97.5%), never rounded to another level.
 */

/** The minus sign of a negative duration (U+2212, not a hyphen). */
const MINUS = '\u2212'

const INTEGER = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 })
const ONE_DECIMAL = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
})
// Percent style shifts the level's shortest round-trip decimal ("0.975", what
// the API sent and the exports carry) two places — exact, unlike level × 100
// in binary (0.57 × 100 is 56.99999999999999).
const CONFIDENCE_LEVEL = new Intl.NumberFormat('en-US', {
  style: 'percent',
  maximumFractionDigits: 20,
})

/**
 * 528.75 → "8h 49m"; 540 → "9h"; 42.4 → "42m"; 0.2 → "0m"; −7.8 → "−8m".
 * A negative value (a confidence interval's lower end for a small group)
 * takes its sign once, before the whole duration (review round 6: −7.8 read
 * "-1h -8m").
 */
export function formatMinutesAsDuration(minutes: number): string {
  const totalMinutes = Math.round(minutes)
  if (totalMinutes < 0) return `${MINUS}${formatMinutesAsDuration(-totalMinutes)}`
  const hours = Math.floor(totalMinutes / 60)
  const rest = totalMinutes % 60
  if (hours === 0) return `${rest}m`
  return rest === 0 ? `${hours}h` : `${hours}h ${rest}m`
}

/** A duration in a sentence: 23.2 → "23 minutes"; 72 → "1 hour 12 minutes"; 0.3 → "under a minute". */
export function formatDurationWords(minutes: number): string {
  const totalMinutes = Math.round(Math.abs(minutes))
  if (totalMinutes === 0) return minutes === 0 ? '0 minutes' : 'under a minute'
  const hours = Math.floor(totalMinutes / 60)
  const rest = totalMinutes % 60
  const minutePart = `${rest} ${rest === 1 ? 'minute' : 'minutes'}`
  if (hours === 0) return minutePart
  const hourPart = `${hours} ${hours === 1 ? 'hour' : 'hours'}`
  return rest === 0 ? hourPart : `${hourPart} ${minutePart}`
}

/** Axis label for a whole-minute tick: 0 → "0"; 30 → "30m"; 60 → "1h"; 90 → "1h 30m"; −15 → "−15m". */
export function formatDurationTick(minutes: number): string {
  if (minutes === 0) return '0'
  if (minutes < 0) return `${MINUS}${formatDurationTick(-minutes)}`
  const hours = Math.floor(minutes / 60)
  const rest = Math.round(minutes % 60)
  if (hours === 0) return `${rest}m`
  return rest === 0 ? `${hours}h` : `${hours}h ${rest}m`
}

/**
 * Axis label for a duration tick on an axis that does not start at zero, in
 * the form every other duration on the page takes: 300 → "5h", 310 →
 * "5h 10m", 45 → "45m" (review round 4: "5h 00m" here beside "5h" in the
 * readout read as two formats). Non-whole-minute ticks (a very narrow axis)
 * keep their decimals.
 */
export function formatClockTick(minutes: number, ticks: readonly number[]): string {
  if (!ticks.every((tick) => Number.isInteger(tick))) return `${ONE_DECIMAL.format(minutes)}m`
  return formatMinutesAsDuration(minutes)
}

/** 528.75 → "528.8". */
export function formatMinutesDetailed(minutes: number): string {
  return ONE_DECIMAL.format(minutes)
}

/**
 * One decimal by default; small nonzero values keep two significant digits
 * instead of collapsing to "0.0" (e.g. 0.0004 → "0.04%").
 */
function adaptiveScaled(value: number): string {
  const rounded = ONE_DECIMAL.format(value)
  if (value !== 0 && rounded === '0.0') return value.toPrecision(2).replace(/0+$/, '')
  if (value !== 0 && rounded === '-0.0') return value.toPrecision(2).replace(/0+$/, '')
  return rounded
}

/** 0.7278 → "72.8%". */
export function formatProportionAsPercent(proportion: number): string {
  return `${adaptiveScaled(proportion * 100)}%`
}

/**
 * A DIFFERENCE of two proportions is percentage points, never "%".
 * −0.023 → "−2.3 pp". Non-proportion differences keep the estimate scale.
 */
export function formatDifferenceValue(value: number, unit: string): string {
  if (unit === 'proportion_of_population') return `${adaptiveScaled(value * 100)} pp`
  return formatValueShort(value, unit)
}

/** A difference of proportions in a sentence: 0.062 → "6.2 percentage points" (or "6.2"). */
export function formatPercentagePoints(value: number, withUnit = true): string {
  const points = adaptiveScaled(value * 100)
  return withUnit ? `${points} percentage points` : points
}

export function formatDifferenceInterval(lower: number, upper: number, unit: string): string {
  if (unit === 'proportion_of_population') {
    return `${adaptiveScaled(lower * 100)} to ${adaptiveScaled(upper * 100)} pp`
  }
  return formatConfidenceInterval(lower, upper, unit)
}

/** 272912483 → "272.9 million"; 41200 → "41,200". */
export function formatPeople(count: number): string {
  if (Math.abs(count) >= 1_000_000) return `${ONE_DECIMAL.format(count / 1_000_000)} million`
  return INTEGER.format(count)
}

/** 2597 → "2,597". */
export function formatCount(count: number): string {
  return INTEGER.format(count)
}

/** Human name of an estimate unit (falls back to the raw unit string). */
export function unitLabel(unit: string): string {
  switch (unit) {
    case 'minutes_per_day':
      return 'minutes per day'
    case 'proportion_of_population':
      return 'share of the population'
    case 'minutes_per_day_of_participants':
      return 'minutes per day among participants'
    case 'persons_per_day':
      return 'people per day'
    default:
      return unit.replaceAll('_', ' ')
  }
}

export interface FormattedEstimate {
  /** Large display form, e.g. "8h 49m" or "72.8%". */
  primary: string
  /** Exact-ish secondary form with unit, e.g. "528.8 minutes per day". */
  secondary: string | null
}

export function formatEstimate(value: number, unit: string): FormattedEstimate {
  switch (unit) {
    case 'minutes_per_day':
      return {
        primary: formatMinutesAsDuration(value),
        secondary: `${formatMinutesDetailed(value)} minutes per day`,
      }
    case 'minutes_per_day_of_participants':
      return {
        primary: formatMinutesAsDuration(value),
        secondary: `${formatMinutesDetailed(value)} minutes per day among participants`,
      }
    case 'proportion_of_population':
      return {
        primary: formatProportionAsPercent(value),
        secondary: 'of the population on an average day',
      }
    case 'persons_per_day':
      return {
        primary: formatPeople(value),
        secondary: 'people on an average day',
      }
    default:
      return { primary: ONE_DECIMAL.format(value), secondary: unitLabel(unit) }
  }
}

/** Short numeric form in the estimate's own scale, for SE / CI / tables / axes. */
export function formatValueShort(value: number, unit: string): string {
  switch (unit) {
    case 'minutes_per_day':
    case 'minutes_per_day_of_participants':
      return `${formatMinutesDetailed(value)} min`
    case 'proportion_of_population':
      return formatProportionAsPercent(value)
    case 'persons_per_day':
      return formatPeople(value)
    default:
      return ONE_DECIMAL.format(value)
  }
}

/** SE in the estimate's scale: percentages become percentage points. */
export function formatStandardError(se: number, unit: string): string {
  if (unit === 'proportion_of_population') return `${adaptiveScaled(se * 100)} pp`
  return formatValueShort(se, unit)
}

export function formatConfidenceInterval(lower: number, upper: number, unit: string): string {
  if (unit === 'proportion_of_population') {
    return `${ONE_DECIMAL.format(lower * 100)}% to ${ONE_DECIMAL.format(upper * 100)}%`
  }
  const upperShort = formatValueShort(upper, unit)
  switch (unit) {
    case 'minutes_per_day':
    case 'minutes_per_day_of_participants':
      return `${formatMinutesDetailed(lower)} to ${upperShort}`
    default:
      return `${formatValueShort(lower, unit)} to ${upperShort}`
  }
}

/**
 * A confidence interval for readers: durations as clock times ("8h 43m to
 * 8h 55m"), or in minutes with one decimal when both ends round to the same
 * minute; other units as `formatConfidenceInterval`.
 */
export function formatReadableInterval(lower: number, upper: number, unit: string): string {
  if (unit === 'minutes_per_day' || unit === 'minutes_per_day_of_participants') {
    const from = formatMinutesAsDuration(lower)
    const to = formatMinutesAsDuration(upper)
    if (from !== to) return `${from} to ${to}`
  }
  return formatConfidenceInterval(lower, upper, unit)
}

/**
 * 0.95 → "95%"; 0.975 → "97.5%"; 0.999 → "99.9%". Every digit is kept: the
 * API accepts any level in (0.5, 1), and rounding would name a different
 * level than the one computed (0.975 as "98%", 0.999 as "100%") (review
 * round 3) — even 0.9999999999999996, a valid level, reads
 * "99.99999999999996%", never "100%".
 */
export function formatConfidenceLevel(level: number): string {
  return CONFIDENCE_LEVEL.format(level)
}

/**
 * What an interval is called on the page: 0.95 → "95% confidence interval".
 * Always by its name, never as a "likely range" (review round 5: a
 * confidence interval is not a range the value is likely to be in).
 */
export function confidenceIntervalName(
  level: number | null | undefined,
  atStart = false,
): string {
  if (level != null) return `${formatConfidenceLevel(level)} confidence interval`
  return atStart ? 'Confidence interval' : 'confidence interval'
}

/** [2025] → "2025"; [2003..2025] → "2003–2025"; [2019, 2021] → "2019, 2021". */
export function formatYears(years: readonly number[]): string {
  if (years.length === 0) return ''
  if (years.length === 1) return String(years[0])
  const sorted = [...years].sort((a, b) => a - b)
  const first = sorted[0] as number
  const last = sorted[sorted.length - 1] as number
  const contiguous = sorted.every((year, index) => year === first + index)
  if (contiguous) return `${first}–${last}`
  return sorted.join(', ')
}

/**
 * Axis tick labels: chooses the fewest decimals at which every label is its
 * tick's exact value (review round 6: a phone's 2.5% steps read "3%" and
 * "8%" when labels only had to differ) and no two labels collide.
 */
export function formatAxisTicks(ticks: readonly number[], unit: string): string[] {
  const millions = (value: number) => unit === 'persons_per_day' && Math.abs(value) >= 1_000_000
  const scaled = (value: number): number =>
    unit === 'proportion_of_population'
      ? value * 100
      : millions(value)
        ? value / 1_000_000
        : value
  const render = (value: number, decimals: number): string => {
    if (unit === 'proportion_of_population') return `${scaled(value).toFixed(decimals)}%`
    if (millions(value)) return `${scaled(value).toFixed(decimals)}M`
    return value.toLocaleString('en-US', {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    })
  }
  const exact = (value: number, decimals: number): boolean => {
    const shown = scaled(value)
    return (
      Math.abs(Number(shown.toFixed(decimals)) - shown) <= 1e-9 * Math.max(1, Math.abs(shown))
    )
  }
  for (let decimals = 0; decimals <= 3; decimals += 1) {
    const labels = ticks.map((tick) => render(tick, decimals))
    if (
      ticks.every((tick) => exact(tick, decimals)) &&
      new Set(labels).size === labels.length
    ) {
      return labels
    }
  }
  return ticks.map((tick) => render(tick, 4))
}

/** A count of people as an axis or a line's end shows it: 277_700_000 → "277.7M"; 41_200 → "41,200". */
export function formatPeopleCompact(count: number): string {
  if (Math.abs(count) >= 1_000_000) return `${ONE_DECIMAL.format(count / 1_000_000)}M`
  return INTEGER.format(count)
}
