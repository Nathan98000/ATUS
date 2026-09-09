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
