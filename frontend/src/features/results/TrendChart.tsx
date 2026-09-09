/**
 * Trend line chart, hand-rolled SVG (see ADR-010).
 *
 * Correctness requirements it exists to guarantee:
 * - unavailable years (e.g. 2020 under the multi-year weights) are a visible,
 *   labeled gap — the line is never drawn across them, never interpolated,
 *   never zero;
 * - confidence intervals are drawn as a band when the API provides them;
 * - every data point is reachable by keyboard (arrow keys) with the same
 *   information as the hover tooltip, announced via a live region.
 */
import { useMemo, useState } from 'react'

import type { TrendPoint } from '../../api/types'
import {
  formatAxisTicks,
  formatConfidenceInterval,
  formatStandardError,
  formatValueShort,
  unitLabel,
} from '../../utils/format'
import { linearScale, niceTicks, padDomain } from './chartScale'

const WIDTH = 760
const HEIGHT = 340
const MARGIN = { top: 18, right: 20, bottom: 42, left: 66 }

interface TrendChartProps {
  points: readonly TrendPoint[]
  unit: string
  /** Y-axis caption, e.g. "Average time per day". */
  measureLabel: string
}

interface AvailablePoint {
  year: number
  value: number
  se: number | null
  ciLower: number | null
  ciUpper: number | null
}

/**
 * Splits the trend into runs of consecutive available points. A run breaks
 * at every unavailable point AND at any jump in years (a spec may request
 * non-contiguous years; the x-scale is linear in calendar time, so drawing
 * across a skipped year would visually interpolate it). Exported for tests.
 */
export function availableSegments(points: readonly TrendPoint[]): AvailablePoint[][] {
  const segments: AvailablePoint[][] = []
  let current: AvailablePoint[] = []
  for (const point of points) {
    if (point.estimate == null) {
      if (current.length > 0) segments.push(current)
      current = []
      continue
    }
    const previous = current[current.length - 1]
    if (previous && point.year !== previous.year + 1) {
      segments.push(current)
      current = []
    }
    current.push({
      year: point.year,
      value: point.estimate.value,
      se: point.estimate.standard_error ?? null,
      ciLower: point.estimate.ci_lower ?? null,
      ciUpper: point.estimate.ci_upper ?? null,
    })
  }
  if (current.length > 0) segments.push(current)
  return segments
}

export function TrendChart({ points, unit, measureLabel }: TrendChartProps) {
  const [activeYear, setActiveYear] = useState<number | null>(null)

  const segments = useMemo(() => availableSegments(points), [points])
  const available = segments.flat()
  const unavailable = points.filter((point) => point.estimate == null)

  if (available.length === 0) {
    return (
      <p className="info-note">
        None of the requested years could be estimated.{' '}
        {unavailable[0]?.unavailable_reason ?? ''}
      </p>
    )
  }

  const years = points.map((point) => point.year)
  const minYear = Math.min(...years)
  const maxYear = Math.max(...years)
  const x = linearScale([minYear, maxYear], [MARGIN.left, WIDTH - MARGIN.right])

  const lows = available.map((point) => point.ciLower ?? point.value)
  const highs = available.map((point) => point.ciUpper ?? point.value)
  const [yMin, yMax] = padDomain(Math.min(...lows), Math.max(...highs))
  const y = linearScale([yMin, yMax], [HEIGHT - MARGIN.bottom, MARGIN.top])

  const yTicks = niceTicks(yMin, yMax, 5)
  const yTickLabels = formatAxisTicks(yTicks, unit)
  const xTickStep = Math.max(1, Math.ceil((maxYear - minYear) / 8))
  const xTicks = years.filter(
    (year) =>
      year === maxYear ||
      ((year - minYear) % xTickStep === 0 && maxYear - year >= xTickStep / 2),
  )

  const halfBand = years.length > 1 ? (x(years[1] as number) - x(years[0] as number)) / 2 : 20

  const active = points.find((point) => point.year === activeYear) ?? null

  const moveActive = (direction: 1 | -1) => {
    const currentIndex =
      activeYear === null ? (direction === 1 ? -1 : years.length) : years.indexOf(activeYear)
    const next = Math.min(Math.max(currentIndex + direction, 0), years.length - 1)
    setActiveYear(years[next] as number)
  }

  const onKeyDown = (event: React.KeyboardEvent) => {
    switch (event.key) {
      case 'ArrowRight':
        event.preventDefault()
        moveActive(1)
        break
      case 'ArrowLeft':
        event.preventDefault()
        moveActive(-1)
        break
      case 'Home':
        event.preventDefault()
        setActiveYear(minYear)
        break
      case 'End':
        event.preventDefault()
        setActiveYear(maxYear)
        break
      case 'Escape':
        setActiveYear(null)
        break
    }
  }

  const onMouseMove = (event: React.MouseEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    const positionX = ((event.clientX - rect.left) / rect.width) * WIDTH
    let nearest: number | null = null
    let nearestDistance = Infinity
    for (const year of years) {
      const distance = Math.abs(x(year) - positionX)
      if (distance < nearestDistance) {
        nearest = year
        nearestDistance = distance
      }
    }
    setActiveYear(nearest)
  }

  const describePoint = (point: TrendPoint): string => {
    if (point.estimate == null) {
      return `${point.year}: unavailable. ${point.unavailable_reason ?? ''}`.trim()
    }
    const { value, standard_error, ci_lower, ci_upper, confidence_level } = point.estimate
    let text = `${point.year}: ${formatValueShort(value, unit)}`
    if (standard_error != null)
      text += `, standard error ${formatStandardError(standard_error, unit)}`
    if (ci_lower != null && ci_upper != null) {
      text += `, ${Math.round((confidence_level ?? 0.95) * 100)}% CI ${formatConfidenceInterval(ci_lower, ci_upper, unit)}`
    }
    return text
  }

  const hasBand = available.some((point) => point.ciLower != null)

  return (
    <div className="trend-chart">
      {/* Interactive chart widget: one tab stop, arrow keys inspect years.
          role="application" is deliberate: it switches screen readers into
          focus mode so the arrow keys reach the widget (a plain group would
          leave arrows to the virtual cursor). The data table below remains
          the primary non-visual representation. */}
      {/* oxlint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
      <div
        role="application"
        // oxlint-disable-next-line jsx-a11y/no-noninteractive-tabindex
        tabIndex={0}
        className="chart-wrap"
        aria-roledescription="interactive chart"
        aria-label={`${measureLabel} by year, ${minYear} to ${maxYear}. Use the left and right arrow keys to read each year's value; Escape dismisses the readout. The full data table is below the chart.`}
        onKeyDown={onKeyDown}
      >
        <svg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          className="chart-svg"
          aria-hidden="true"
          onMouseMove={onMouseMove}
          onMouseLeave={() => setActiveYear(null)}
        >
          {/* Gridlines + y axis */}
          {yTicks.map((tick, tickIndex) => (
            <g key={tick}>
              <line
                x1={MARGIN.left}
                x2={WIDTH - MARGIN.right}
                y1={y(tick)}
                y2={y(tick)}
                className="chart-grid"
              />
              <text x={MARGIN.left - 8} y={y(tick)} className="chart-tick chart-tick--y">
                {yTickLabels[tickIndex]}
              </text>
            </g>
          ))}
          {/* X axis ticks */}
          {xTicks.map((year) => (
            <text key={year} x={x(year)} y={HEIGHT - MARGIN.bottom + 20} className="chart-tick">
              {year}
            </text>
          ))}
          <text
            x={MARGIN.left}
            y={MARGIN.top - 6}
            className="chart-axis-label"
          >{`${measureLabel} (${unitLabel(unit)})`}</text>

          {/* Unavailable-year bands: an explicit, labeled gap. */}
          {unavailable.map((point) => (
            <g key={point.year} data-testid={`unavailable-${point.year}`}>
              <rect
                x={x(point.year) - halfBand}
                y={MARGIN.top}
                width={halfBand * 2}
                height={HEIGHT - MARGIN.top - MARGIN.bottom}
                className="chart-unavailable-band"
              />
              <text
                transform={`translate(${x(point.year) + 4}, ${MARGIN.top + 12}) rotate(90)`}
                className="chart-unavailable-label"
              >
                {point.year} — no estimate
              </text>
            </g>
          ))}

          {/* Confidence band per contiguous segment */}
          {hasBand
            ? segments.map((segment, segmentIndex) =>
                segment.length > 0 && segment.every((point) => point.ciLower != null) ? (
                  <polygon
                    key={segmentIndex}
                    className="chart-ci-band"
                    data-testid="ci-band"
                    points={[
                      ...segment.map(
                        (point) => `${x(point.year)},${y(point.ciUpper as number)}`,
                      ),
                      ...[...segment]
                        .reverse()
                        .map((point) => `${x(point.year)},${y(point.ciLower as number)}`),
                    ].join(' ')}
                  />
                ) : null,
              )
            : null}

          {/* One polyline per contiguous segment — never across a gap. */}
          {segments.map((segment, segmentIndex) =>
            segment.length > 1 ? (
              <polyline
                key={segmentIndex}
                className="chart-line"
                data-testid="trend-line-segment"
                points={segment.map((point) => `${x(point.year)},${y(point.value)}`).join(' ')}
              />
            ) : null,
          )}
          {available.map((point) => (
            <circle
              key={point.year}
              cx={x(point.year)}
              cy={y(point.value)}
              r={point.year === activeYear ? 5 : 3.2}
              className="chart-point"
              data-testid={`point-${point.year}`}
            />
          ))}

          {/* Active-year guide */}
          {activeYear !== null ? (
            <line
              x1={x(activeYear)}
              x2={x(activeYear)}
              y1={MARGIN.top}
              y2={HEIGHT - MARGIN.bottom}
              className="chart-guide"
            />
          ) : null}
        </svg>
        {active ? (
          <div
            className="chart-tooltip"
            style={{
              left: `${(x(active.year) / WIDTH) * 100}%`,
              translate: x(active.year) > WIDTH * 0.72 ? '-100% 0' : '0 0',
            }}
          >
            <b>{active.year}</b>
            {active.estimate == null ? (
              <span>Unavailable — {active.unavailable_reason}</span>
            ) : (
              <>
                <span>{formatValueShort(active.estimate.value, unit)}</span>
                {active.estimate.standard_error != null ? (
                  <span>SE {formatStandardError(active.estimate.standard_error, unit)}</span>
                ) : null}
                {active.estimate.ci_lower != null && active.estimate.ci_upper != null ? (
                  <span>
                    {Math.round((active.estimate.confidence_level ?? 0.95) * 100)}% CI{' '}
                    {formatConfidenceInterval(
                      active.estimate.ci_lower,
                      active.estimate.ci_upper,
                      unit,
                    )}
                  </span>
                ) : null}
                {active.n_respondents != null ? (
                  <span>n = {active.n_respondents.toLocaleString('en-US')}</span>
                ) : null}
              </>
            )}
          </div>
        ) : null}
        <p className="visually-hidden" aria-live="polite">
          {active ? describePoint(active) : ''}
        </p>
      </div>
      {unavailable.length > 0 ? (
        <p className="field-hint chart-gap-note" data-testid="gap-note">
          {unavailable.map((point) => (
            <span key={point.year}>
              <b>{point.year} has no estimate:</b> {point.unavailable_reason}{' '}
            </span>
          ))}
        </p>
      ) : null}
      {hasBand ? (
        <p className="field-hint">
          The shaded band is the confidence interval around each year's estimate.
        </p>
      ) : null}
    </div>
  )
}
