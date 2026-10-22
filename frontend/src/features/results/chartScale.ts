/** Tiny presentation-scale helpers for the SVG charts (no statistics). */

export interface LinearScale {
  (value: number): number
  domain: [number, number]
}

export function linearScale(domain: [number, number], range: [number, number]): LinearScale {
  const [d0, d1] = domain
  const [r0, r1] = range
  const span = d1 - d0 || 1
  const scale = ((value: number) => r0 + ((value - d0) / span) * (r1 - r0)) as LinearScale
  scale.domain = domain
  return scale
}

/** Round, human-friendly axis ticks covering [min, max]. */
export function niceTicks(min: number, max: number, targetCount = 5): number[] {
  if (min === max) {
    max = min + 1
  }
  const rawStep = (max - min) / targetCount
  const magnitude = 10 ** Math.floor(Math.log10(rawStep))
  const candidates = [1, 2, 2.5, 5, 10]
  const step =
    (candidates.find((candidate) => candidate * magnitude >= rawStep) ?? 10) * magnitude
  const first = Math.ceil(min / step) * step
  const ticks: number[] = []
  for (let tick = first; tick <= max + step / 1e6; tick += step) {
    ticks.push(Number(tick.toFixed(10)))
  }
  return ticks
}

/** Pads a [min, max] domain so points/bands don't touch the chart edges. */
export function padDomain(min: number, max: number, fraction = 0.12): [number, number] {
  if (min === max) {
    const pad = Math.abs(min) * fraction || 1
    return [min - pad, max + pad]
  }
  const pad = (max - min) * fraction
  return [min - pad, max + pad]
}

/** Round ticks from zero up to at least `max` (bar charts start at zero). */
export function ticksFromZero(max: number, targetCount = 4): number[] {
  const ticks = niceTicks(0, max, targetCount)
  const last = ticks[ticks.length - 1] ?? 0
  const step = (ticks[1] ?? 1) - (ticks[0] ?? 0)
  if (last < max) ticks.push(Number((last + step).toFixed(10)))
  return ticks
}

/** Durations read best on clock-like steps (15 min, 30 min, 1 h, 2 h…). */
const DURATION_STEPS = [1, 2, 5, 10, 15, 20, 30, 60, 90, 120, 180, 240, 360, 480, 720]

/** Ticks in minutes from zero up to at least `max`, on clock-like steps. */
export function durationTicksFromZero(max: number, maxCount = 5): number[] {
  const target = Math.max(max, 1)
  const step =
    DURATION_STEPS.find((candidate) => Math.ceil(target / candidate) <= maxCount) ??
    Math.ceil(target / maxCount / 60) * 60
  const count = Math.ceil(target / step)
  return Array.from({ length: count + 1 }, (_, index) => index * step)
}
