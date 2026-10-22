/**
 * Trend result: the answer in one sentence over the chart, its small print,
 * and the result's notes. A visually hidden table gives screen readers every
 * year's value. A trend split by group (`SplitTrendView`) draws one line per
 * group. 2020, which the multi-year weights cannot estimate, comes from its
 * partial-year estimate when the page has one (`partial2020.ts`): a hollow
 * point with a note; a year with no estimate at all stays a labeled gap.
 */
import { Link } from 'react-router'

import type {
  EstimateResponse,
  PopulationRequest,
  TrendPoint,
  TrendResponse,
} from '../../api/types'
import { WarningBanner } from '../../components/WarningBanner'
import { interpretSplitTrend, interpretTrend, measureName } from '../../domain/describe'
import { contextNotes, presentUnavailableReason } from '../../domain/notes'
import { formatEstimate, formatLikelyRange, unitLabel } from '../../utils/format'
import { ChartFootnote } from './ChartFootnote'
import { PARTIAL_YEAR, withPartial2020 } from './partial2020'
import { TrendChart } from './TrendChart'

/** The small print for a 2020 drawn from its partial-year estimate. */
function PartialNote() {
  return (
    <span data-testid="partial-note">
      <span className="chart-key__swatch chart-key__swatch--partial" aria-hidden="true" />
      <b>{PARTIAL_YEAR} (hollow point) is a partial year:</b> the survey paused from March 18 to
      May 9 because of COVID-19, so it may not compare exactly with other years.
    </span>
  )
}

/** Why a year has no estimate, in the small print. */
function GapNote({ point }: { point: TrendPoint }) {
  return (
    <span data-testid="gap-note">
      <b>No data for {point.year}:</b> {presentUnavailableReason(point.unavailable_reason)}
    </span>
  )
}

function yearHeader(year: number, partialYears: readonly number[]): string {
  return partialYears.includes(year) ? `${year} (partial year)` : String(year)
}

export function TrendView({
  result,
  partial2020,
  note,
}: {
  result: TrendResponse
  /** 2020 alone under the pandemic weights, when the trend's weights have none. */
  partial2020?: EstimateResponse
  /** What a link's own labels stand for (`definitionNote`), if anything. */
  note?: string | null
}) {
  const { points, partialYears } = withPartial2020(result.points, partial2020)
  const firstEstimate = points.find((point) => point.estimate != null)?.estimate
  const unit = firstEstimate?.unit ?? 'minutes_per_day'
  const unavailable = points.filter((point) => point.estimate == null)
  const measureLabel = measureName(result.measure)
  // The answer reads full-year estimates only: the trend's own points.
  const interpretation = interpretTrend(result, partialYears)
  const hasBand = points.some((point) => point.estimate?.ci_lower != null)

  return (
    <div className="result-stack">
      <section className="card result-card" aria-label="Trend">
        {interpretation ? <p className="result-lead">{interpretation}</p> : null}
        <TrendChart
          series={[{ label: measureLabel, points, partialYears }]}
          unit={unit}
          measureLabel={measureLabel}
        />
        <ChartFootnote
          range={
            hasBand
              ? { mark: 'band', level: firstEstimate?.confidence_level ?? null }
              : undefined
          }
          notes={[
            ...(partialYears.length > 0 ? [<PartialNote key="partial" />] : []),
            ...unavailable.map((point) => <GapNote key={point.year} point={point} />),
            ...(note != null ? [note] : []),
          ]}
          context={contextNotes(result.warnings)}
        />
        {/* Hidden by its wrapper: a table ignores the 1px width, and would
            widen the page on a phone. */}
        <div className="visually-hidden">
          <table>
            <caption>
              {measureLabel} by year ({unitLabel(unit)})
            </caption>
            <thead>
              <tr>
                <th scope="col">Year</th>
                <th scope="col">Estimate</th>
                <th scope="col">Likely range</th>
              </tr>
            </thead>
            <tbody>
              {points.map((point) => (
                <tr key={point.year}>
                  <th scope="row">{yearHeader(point.year, partialYears)}</th>
                  {point.estimate == null ? (
                    <td colSpan={2}>
                      No data. {presentUnavailableReason(point.unavailable_reason)}
                    </td>
                  ) : (
                    <>
                      <td>{formatEstimate(point.estimate.value, unit).primary}</td>
                      <td>
                        {point.estimate.ci_lower != null && point.estimate.ci_upper != null
                          ? formatLikelyRange(
                              point.estimate.ci_lower,
                              point.estimate.ci_upper,
                              unit,
                            )
                          : '—'}
                      </td>
                    </>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <WarningBanner warnings={result.warnings} />
    </div>
  )
}

/** One group's line of a split trend: its name and its own trend. */
export interface SplitSeries {
  label: string
  /** The group's own filter (its label is the plain description of it). */
  filters?: PopulationRequest
  result: TrendResponse
  /** The group's 2020 under the pandemic weights, when its trend has none. */
  partial2020?: EstimateResponse
}

export function SplitTrendView({
  series,
  legendTitle,
  note,
  comparison,
}: {
  series: readonly SplitSeries[]
  /** What the groups differ in, e.g. "Sex". */
  legendTitle: string
  note?: string | null
  /**
   * Two of the groups compared in one year, where a comparison shows the
   * likely range a split trend does not draw (review round 4).
   */
  comparison?: { path: string; text: string } | null
}) {
  const first = series[0]?.result
  if (first === undefined) return null
  const lines = series.map(({ label, result, partial2020 }) => ({
    label,
    ...withPartial2020(result.points, partial2020),
  }))
  const unit =
    lines.flatMap((line) => line.points).find((point) => point.estimate != null)?.estimate
      ?.unit ?? 'minutes_per_day'
  const measureLabel = measureName(first.measure)
  const interpretation = interpretSplitTrend(series)
  const years = first.points.map((point) => point.year)
  const partialYears = [...new Set(lines.flatMap((line) => line.partialYears))]
  // A year no group has an estimate for is the chart's gap; say why once.
  const gaps = years.flatMap((year) => {
    const points = lines.map((line) => line.points.find((point) => point.year === year))
    const point = points[0]
    return point && points.every((candidate) => candidate?.estimate == null) ? [point] : []
  })
  const warnings = [...new Set(series.flatMap(({ result }) => result.warnings))]

  return (
    <div className="result-stack">
      <section className="card result-card" aria-label="Trend by group">
        {interpretation ? <p className="result-lead">{interpretation}</p> : null}
        <TrendChart
          series={lines}
          unit={unit}
          measureLabel={measureLabel}
          legendTitle={legendTitle}
        />
        {comparison ? (
          <p className="split-comparison">
            <Link to={comparison.path}>{comparison.text}</Link>
          </p>
        ) : null}
        <ChartFootnote
          notes={[
            ...(partialYears.length > 0 ? [<PartialNote key="partial" />] : []),
            ...gaps.map((point) => <GapNote key={point.year} point={point} />),
            'Lines show estimates from a sample of people. For their likely range, compare two groups in one year.',
            ...(note != null ? [note] : []),
          ]}
          context={contextNotes(warnings)}
        />
        <div className="visually-hidden">
          <table>
            <caption>
              {measureLabel} by year and {legendTitle.toLowerCase()} ({unitLabel(unit)})
            </caption>
            <thead>
              <tr>
                <th scope="col">Year</th>
                {lines.map(({ label }) => (
                  <th key={label} scope="col">
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {years.map((year) => (
                <tr key={year}>
                  <th scope="row">{yearHeader(year, partialYears)}</th>
                  {lines.map(({ label, points }) => {
                    const estimate = points.find((point) => point.year === year)?.estimate
                    return (
                      <td key={label}>
                        {estimate != null
                          ? formatEstimate(estimate.value, unit).primary
                          : 'No data'}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <WarningBanner warnings={warnings} />
    </div>
  )
}
