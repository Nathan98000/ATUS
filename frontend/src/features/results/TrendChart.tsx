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
 *
 * The look: 2.5px lines over hairline gridlines, ringed points, clock-like
 * duration ticks. One trend gets its band and its latest value labeled at
 * the end of the line. A trend split into groups draws one line per group —
 * each its own colour and dash pattern, named in a legend below, without
 * bands — and the readout lists every group at the year under the cursor.
 * The readout says values only; the band carries the uncertainty.
 */
import { useMemo, useState } from 'react'

import type { TrendPoint } from '../../api/types'
import { presentUnavailableReason, shortUnavailableReason } from '../../domain/notes'
import { formatAxisTicks, formatClockTick, formatEstimate } from '../../utils/format'
import { durationTicks, linearScale, niceTicks, padDomain } from './chartScale'
import { useElementWidth } from './useElementWidth'

/** Drawn at the container's own width, so text keeps its CSS size. */
const DEFAULT_WIDTH = 760
const MIN_WIDTH = 280
/** The right margin holds the latest value's label. */
const MARGIN = { top: 22, right: 64, bottom: 34, left: 58 }
const NARROW_MARGIN = { top: 22, right: 52, bottom: 34, left: 50 }
/** Room each x-axis year label needs. */
const MIN_YEAR_TICK_SPACING = 52
/**
 * Each line's dash pattern, in series order, so identity never rests on
 * colour alone (the colours are `--series-1`…`--series-6`).
 */
const SERIES_DASHES = ['none', '7 5', '2 4', '10 4 2 4', '14 6', '4 3']

/** One line: a whole trend, or one group of a split trend. */
export interface TrendSeries {
  label: string
  points: readonly TrendPoint[]
}

interface TrendChartProps {
  series: readonly TrendSeries[]
  unit: string
  /** What the lines measure, e.g. "Average time per day" (for screen readers). */
  measureLabel: string
  /** A split trend's legend title, e.g. "Sex". */
  legendTitle?: string
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

function isDuration(unit: string): boolean {
  return unit === 'minutes_per_day' || unit === 'minutes_per_day_of_participants'
}

/** The value as the page shows it: "5h 10m", "72.8%", "41.2 million". */
function showValue(value: number, unit: string): string {
  return formatEstimate(value, unit).primary
}

/** A short stroke of a line's colour and dash, with its point: legend and readout keys. */
export function SeriesKey({ index }: { index: number }) {
  return (
    <svg
      className={`series-key chart-series--${index + 1}`}
      viewBox="0 0 30 10"
      aria-hidden="true"
    >
      <line
        x1="1"
        x2="29"
        y1="5"
        y2="5"
        className="chart-line"
        strokeDasharray={SERIES_DASHES[index] ?? 'none'}
      />
      <circle cx="15" cy="5" r="3" className="chart-point" />
    </svg>
  )
}

export function TrendChart({ series, unit, measureLabel, legendTitle }: TrendChartProps) {
  const [activeYear, setActiveYear] = useState<number | null>(null)
  const [containerRef, measuredWidth] = useElementWidth<HTMLDivElement>(DEFAULT_WIDTH)
  const width = Math.max(measuredWidth, MIN_WIDTH)
  const narrow = width < 520
  const height = narrow ? 260 : 360
  const margin = narrow ? NARROW_MARGIN : MARGIN
  const single = series.length === 1

  const lines = useMemo(
    () =>
      series.map((line) => {
        const segments = availableSegments(line.points)
        return { ...line, segments, available: segments.flat() }
      }),
    [series],
  )
  const allPoints = series.flatMap((line) => line.points)
  const available = lines.flatMap((line) => line.available)

  if (available.length === 0) {
    const reason = allPoints.find((point) => point.estimate == null)?.unavailable_reason
    return (
      <p className="info-note">
        None of the requested years could be estimated. {presentUnavailableReason(reason)}
      </p>
    )
  }

  const years = [...new Set(allPoints.map((point) => point.year))].sort((a, b) => a - b)
  // A year is a gap of the chart when no line has a value for it.
  const unavailable = years.flatMap((year) => {
    const points = allPoints.filter((point) => point.year === year)
    return points.every((point) => point.estimate == null) ? [points[0] as TrendPoint] : []
  })
  const minYear = years[0] as number
  const maxYear = years[years.length - 1] as number
  // A little room on both sides so the first and last points clear the edges.
  const yearPad = maxYear > minYear ? 0.4 : 1
  const x = linearScale(
    [minYear - yearPad, maxYear + yearPad],
    [margin.left, width - margin.right],
  )

  const lows = available.map((point) => (single ? (point.ciLower ?? point.value) : point.value))
  const highs = available.map((point) =>
    single ? (point.ciUpper ?? point.value) : point.value,
  )
  const [yMin, yMax] = padDomain(Math.min(...lows), Math.max(...highs))
  const y = linearScale([yMin, yMax], [height - margin.bottom, margin.top])

  const yTickCount = narrow ? 4 : 5
  const yTicks = isDuration(unit)
    ? durationTicks(yMin, yMax, yTickCount)
    : niceTicks(yMin, yMax, yTickCount)
  const yTickLabels = isDuration(unit)
    ? yTicks.map((tick) => formatClockTick(tick, yTicks))
    : formatAxisTicks(yTicks, unit)
  const maxXTicks = Math.max(
    2,
    Math.floor((width - margin.left - margin.right) / MIN_YEAR_TICK_SPACING),
  )
  const xTickStep = Math.max(1, Math.ceil((maxYear - minYear) / Math.min(8, maxXTicks)))
  const xTicks = years.filter(
    (year) =>
      year === maxYear ||
      ((year - minYear) % xTickStep === 0 && maxYear - year >= xTickStep / 2),
  )

  const yearWidth = years.length > 1 ? x(minYear + 1) - x(minYear) : 40
  const plotTop = margin.top
  const plotBottom = height - margin.bottom

  /** Each line's point for a year (null: that line has no estimate then). */
  const at = (year: number) =>
    lines.map((line) => line.points.find((point) => point.year === year) ?? null)
  const last = lines[0]?.available[lines[0].available.length - 1]

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
    const positionX = ((event.clientX - rect.left) / rect.width) * width
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

  const describeYear = (year: number): string => {
    const points = at(year)
    if (single) {
      const point = points[0]
      if (point?.estimate == null) {
        return `${year}: no estimate. ${presentUnavailableReason(point?.unavailable_reason)}`
      }
      return `${year}: ${showValue(point.estimate.value, unit)}`
    }
    if (points.every((point) => point?.estimate == null)) {
      return `${year}: no estimate. ${presentUnavailableReason(points[0]?.unavailable_reason)}`
    }
    const parts = points.map((point, index) =>
      point?.estimate == null
        ? `${lines[index]?.label} no estimate`
        : `${lines[index]?.label} ${showValue(point.estimate.value, unit)}`,
    )
    return `${year}: ${parts.join(', ')}`
  }

  const hasBand = single && available.some((point) => point.ciLower != null)
  const activePoints = activeYear !== null ? at(activeYear) : []
  const activeValues = activePoints.flatMap((point) =>
    point?.estimate != null ? [point.estimate.value] : [],
  )
  const activeGap = activeYear !== null && activeValues.length === 0
  // The readout sits beside the year, near its highest value, on the side
  // with more room.
  const tooltipLeft = activeYear !== null ? x(activeYear) : 0
  const tooltipFlip = tooltipLeft > width * 0.62
  const tooltipTop =
    activeValues.length > 0 ? Math.max(plotTop, y(Math.max(...activeValues)) - 58) : plotTop

  return (
    <div className="trend-chart" ref={containerRef}>
      {/* Interactive chart widget: one tab stop, arrow keys inspect years.
          role="application" is deliberate: it switches screen readers into
          focus mode so the arrow keys reach the widget (a plain group would
          leave arrows to the virtual cursor). A visually hidden table of
          every year follows the chart for screen readers. */}
      {/* oxlint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
      <div
        role="application"
        // oxlint-disable-next-line jsx-a11y/no-noninteractive-tabindex
        tabIndex={0}
        className="chart-wrap"
        aria-roledescription="interactive chart"
        aria-label={`${measureLabel} by year, ${minYear} to ${maxYear}${
          single ? '' : `, one line each for ${lines.map((line) => line.label).join(', ')}`
        }. Use the left and right arrow keys to read each year's value; Escape dismisses the readout.`}
        onKeyDown={onKeyDown}
      >
        <svg
          viewBox={`0 0 ${width} ${height}`}
          width={width}
          height={height}
          className="chart-svg"
          aria-hidden="true"
          onMouseMove={onMouseMove}
          onMouseLeave={() => setActiveYear(null)}
        >
          {/* Unavailable years: a quiet, labeled gap behind the data. */}
          {unavailable.map((point) => (
            <g key={point.year} data-testid={`unavailable-${point.year}`}>
              <rect
                x={x(point.year) - yearWidth / 2}
                y={plotTop}
                width={yearWidth}
                height={plotBottom - plotTop}
                className="chart-unavailable-band"
              />
              <text x={x(point.year)} y={plotTop - 8} className="chart-unavailable-label">
                No data
              </text>
            </g>
          ))}

          {/* Gridlines + y axis */}
          {yTicks.map((tick, tickIndex) => (
            <g key={tick}>
              <line
                x1={margin.left}
                x2={width - margin.right}
                y1={y(tick)}
                y2={y(tick)}
                className="chart-grid"
              />
              <text x={margin.left - 10} y={y(tick)} className="chart-tick chart-tick--y">
                {yTickLabels[tickIndex]}
              </text>
            </g>
          ))}
          {/* X axis: a baseline and the year labels */}
          <line
            x1={margin.left}
            x2={width - margin.right}
            y1={plotBottom}
            y2={plotBottom}
            className="chart-baseline"
          />
          {xTicks.map((year) => (
            <text key={year} x={x(year)} y={plotBottom + 22} className="chart-tick">
              {year}
            </text>
          ))}

          {/* Confidence band per contiguous segment (one trend only) */}
          {hasBand
            ? lines[0]?.segments.map((segment, segmentIndex) =>
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

          {/* Active-year guide, behind the lines */}
          {activeYear !== null ? (
            <line
              x1={x(activeYear)}
              x2={x(activeYear)}
              y1={plotTop}
              y2={plotBottom}
              className="chart-guide"
            />
          ) : null}

          {/* One polyline per contiguous segment — never across a gap. */}
          {lines.map((line, lineIndex) => (
            <g
              key={line.label + lineIndex}
              className={`chart-series chart-series--${lineIndex + 1}`}
              data-testid={single ? undefined : `series-${lineIndex}`}
            >
              {line.segments.map((segment, segmentIndex) =>
                segment.length > 1 ? (
                  <polyline
                    key={segmentIndex}
                    className="chart-line"
                    data-testid="trend-line-segment"
                    strokeDasharray={SERIES_DASHES[lineIndex] ?? 'none'}
                    points={segment
                      .map((point) => `${x(point.year)},${y(point.value)}`)
                      .join(' ')}
                  />
                ) : null,
              )}
              {line.available.map((point) => (
                <circle
                  key={point.year}
                  cx={x(point.year)}
                  cy={y(point.value)}
                  r={point.year === activeYear ? 6 : single && point === last ? 5 : 4}
                  className="chart-point"
                  data-testid={single ? `point-${point.year}` : undefined}
                />
              ))}
            </g>
          ))}

          {/* One trend: its latest value, labeled at the end of the line. */}
          {single && last ? (
            <text
              x={x(last.year) + 10}
              y={y(last.value)}
              className="chart-end-label"
              data-testid="end-label"
            >
              {showValue(last.value, unit)}
            </text>
          ) : null}
        </svg>
        {activeYear !== null ? (
          <div
            className="chart-tooltip"
            style={{
              left: `${tooltipLeft}px`,
              top: `${tooltipTop}px`,
              translate: tooltipFlip ? 'calc(-100% - 12px) 0' : '12px 0',
            }}
          >
            {activeGap ? (
              <>
                <span className="chart-tooltip__label">{activeYear}</span>
                <span className="chart-tooltip__wrap">
                  No data: {shortUnavailableReason(activePoints[0]?.unavailable_reason)}
                </span>
              </>
            ) : single ? (
              <>
                <span className="chart-tooltip__value">
                  {showValue(activeValues[0] as number, unit)}
                </span>
                <span className="chart-tooltip__label">{activeYear}</span>
              </>
            ) : (
              <>
                <span className="chart-tooltip__label">{activeYear}</span>
                {activePoints.map((point, index) => (
                  <span key={index} className="chart-tooltip__row">
                    <SeriesKey index={index} />
                    <b>
                      {point?.estimate != null ? showValue(point.estimate.value, unit) : '—'}
                    </b>
                    <span>{lines[index]?.label}</span>
                  </span>
                ))}
              </>
            )}
          </div>
        ) : null}
        <p className="visually-hidden" aria-live="polite">
          {activeYear !== null ? describeYear(activeYear) : ''}
        </p>
      </div>
      {single ? null : (
        <div className="chart-legend" aria-hidden="true">
          {legendTitle ? <span className="chart-legend__title">{legendTitle}</span> : null}
          {lines.map((line, index) => (
            <span key={line.label + index} className="chart-legend__item">
              <SeriesKey index={index} />
              {line.label}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
