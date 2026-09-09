/**
 * Presentation-only number formatting. These functions transform units for
 * display ("528.75" → "8h 49m"); they never change, round, or derive the
 * underlying data — raw API values stay untouched in state, and exact values
 * are always available in each result's data table.
 *
 * Precision policy (documented in docs/frontend.md):
 * - durations: hours+minutes rounded to the nearest minute; detailed form
 *   shows one decimal of minutes;
 * - percentages: one decimal;
 * - standard errors and confidence bounds: same precision as the detailed
 *   estimate they qualify;
 * - people counts: millions with one decimal above 1M, thousands separators
 *   below;
 * - sample sizes: exact integers with thousands separators.
 */

const INTEGER = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 })
const ONE_DECIMAL = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
})

/** 528.75 → "8h 49m"; 42.4 → "42m"; 0.2 → "0m". */
export function formatMinutesAsDuration(minutes: number): string {
  const totalMinutes = Math.round(minutes)
  const hours = Math.floor(totalMinutes / 60)
  const rest = totalMinutes % 60
  if (hours === 0) return `${rest}m`
  return `${hours}h ${rest}m`
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

/** 0.95 → "95%". */
export function formatConfidenceLevel(level: number): string {
  return `${INTEGER.format(level * 100)}%`
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
 * Axis tick labels: chooses the fewest decimals that keep every label
 * distinct (whole percents can collide when ticks are sub-point apart).
 */
export function formatAxisTicks(ticks: readonly number[], unit: string): string[] {
  const render = (value: number, decimals: number): string => {
    if (unit === 'proportion_of_population') return `${(value * 100).toFixed(decimals)}%`
    if (unit === 'persons_per_day' && Math.abs(value) >= 1_000_000) {
      return `${(value / 1_000_000).toFixed(decimals)}M`
    }
    return value.toLocaleString('en-US', {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    })
  }
  for (let decimals = 0; decimals <= 3; decimals += 1) {
    const labels = ticks.map((tick) => render(tick, decimals))
    if (new Set(labels).size === labels.length) return labels
  }
  return ticks.map((tick) => render(tick, 4))
}
