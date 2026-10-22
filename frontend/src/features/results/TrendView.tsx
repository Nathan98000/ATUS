/**
 * Trend result: the answer in one sentence over the chart (with explicit
 * unavailable-year gaps), its small print, and the result's notes. A
 * visually hidden table gives screen readers every year's value. A trend
 * split by group (`SplitTrendView`) draws one line per group.
 */
import type { TrendResponse } from '../../api/types'
import { WarningBanner } from '../../components/WarningBanner'
import { interpretSplitTrend, interpretTrend, measureName } from '../../domain/describe'
import { contextNotes, presentUnavailableReason } from '../../domain/notes'
import { formatEstimate, formatLikelyRange, unitLabel } from '../../utils/format'
import { ChartFootnote } from './ChartFootnote'
import { TrendChart } from './TrendChart'

export function TrendView({
  result,
  note,
}: {
  result: TrendResponse
  /** What a link's own labels stand for (`definitionNote`), if anything. */
  note?: string | null
}) {
  const firstEstimate = result.points.find((point) => point.estimate != null)?.estimate
  const unit = firstEstimate?.unit ?? 'minutes_per_day'
  const unavailable = result.points.filter((point) => point.estimate == null)
  const measureLabel = measureName(result.measure)
  const interpretation = interpretTrend(result)
  const hasBand = result.points.some((point) => point.estimate?.ci_lower != null)

  return (
    <div className="result-stack">
      <section className="card result-card" aria-label="Trend">
        {interpretation ? <p className="result-lead">{interpretation}</p> : null}
        <TrendChart
          series={[{ label: measureLabel, points: result.points }]}
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
            ...unavailable.map((point) => (
              <span key={point.year} data-testid="gap-note">
                <b>No data for {point.year}:</b>{' '}
                {presentUnavailableReason(point.unavailable_reason)}
              </span>
            )),
            ...(note != null ? [note] : []),
            ...contextNotes(result.warnings),
          ]}
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
              {result.points.map((point) => (
                <tr key={point.year}>
                  <th scope="row">{point.year}</th>
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
  result: TrendResponse
}

export function SplitTrendView({
  series,
  legendTitle,
  note,
}: {
  series: readonly SplitSeries[]
  /** What the groups differ in, e.g. "Sex". */
  legendTitle: string
  note?: string | null
}) {
  const first = series[0]?.result
  if (first === undefined) return null
  const unit =
    series.flatMap(({ result }) => result.points).find((point) => point.estimate != null)
      ?.estimate?.unit ?? 'minutes_per_day'
  const measureLabel = measureName(first.measure)
  const interpretation = interpretSplitTrend(series)
  const years = first.points.map((point) => point.year)
  // A year no group has an estimate for is the chart's gap; say why once.
  const gaps = years.flatMap((year) => {
    const points = series.map(({ result }) =>
      result.points.find((point) => point.year === year),
    )
    return points.every((point) => point?.estimate == null) ? [points[0]] : []
  })
  const warnings = [...new Set(series.flatMap(({ result }) => result.warnings))]

  return (
    <div className="result-stack">
      <section className="card result-card" aria-label="Trend by group">
        {interpretation ? <p className="result-lead">{interpretation}</p> : null}
        <TrendChart
          series={series.map(({ label, result }) => ({ label, points: result.points }))}
          unit={unit}
          measureLabel={measureLabel}
          legendTitle={legendTitle}
        />
        <ChartFootnote
          notes={[
            ...gaps.map((point) =>
              point ? (
                <span key={point.year} data-testid="gap-note">
                  <b>No data for {point.year}:</b>{' '}
                  {presentUnavailableReason(point.unavailable_reason)}
                </span>
              ) : null,
            ),
            'Each line is one group’s estimate. The survey asks a sample of people, so each figure is an estimate; likely ranges are drawn when a trend shows one group.',
            ...(note != null ? [note] : []),
            ...contextNotes(warnings),
          ]}
        />
        <div className="visually-hidden">
          <table>
            <caption>
              {measureLabel} by year and {legendTitle.toLowerCase()} ({unitLabel(unit)})
            </caption>
            <thead>
              <tr>
                <th scope="col">Year</th>
                {series.map(({ label }) => (
                  <th key={label} scope="col">
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {years.map((year) => (
                <tr key={year}>
                  <th scope="row">{year}</th>
                  {series.map(({ label, result }) => {
                    const estimate = result.points.find(
                      (point) => point.year === year,
                    )?.estimate
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
