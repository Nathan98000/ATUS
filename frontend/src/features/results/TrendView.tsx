/**
 * Trend result: the chart (with explicit unavailable-year gaps), an
 * accessible data table with exact values, warnings, and methodology.
 */
import type { TrendResponse } from '../../api/types'
import { WarningBanner } from '../../components/WarningBanner'
import {
  formatConfidenceInterval,
  formatCount,
  formatStandardError,
  formatValueShort,
  formatYears,
  unitLabel,
} from '../../utils/format'
import { MethodologyPanel, type MethodologyRow } from './MethodologyPanel'
import { TrendChart } from './TrendChart'

const MEASURE_SHORT_LABELS: Record<string, string> = {
  average_minutes_per_day: 'Average time per day',
  participation_rate: 'Participation rate',
  average_minutes_per_participant: 'Time among participants',
  participants_per_day: 'People per day',
}

export function trendMethodologyRows(result: TrendResponse): MethodologyRow[] {
  const years = result.points.map((point) => point.year)
  return [
    {
      term: 'Activity',
      detail: `${result.activity.label} — lexicon codes ${result.activity.include.join(', ')}${
        result.activity.exclude.length > 0
          ? ` excluding ${result.activity.exclude.join(', ')}`
          : ''
      } (${result.activity.leaf_code_count} six-digit activities)`,
    },
    { term: 'Population', detail: result.population },
    { term: 'Statistic', detail: result.measure.replaceAll('_', ' ') },
    { term: 'Years', detail: `${formatYears(years)}, estimated independently per year` },
    {
      term: 'Weighting',
      detail: `${result.weight.bls_variable} (${result.weight.scheme}) — official BLS survey weights.`,
    },
    {
      term: 'Uncertainty',
      detail:
        result.variance_method === 'replicate'
          ? 'Standard errors from the official 160 replicate weights, computed per year.'
          : 'None requested (point estimates only).',
    },
    { term: 'Analytics version', detail: result.analytics_version },
  ]
}

export function TrendView({
  result,
  analysisKey,
}: {
  result: TrendResponse
  analysisKey: string | null
}) {
  const unit =
    result.points.find((point) => point.estimate != null)?.estimate?.unit ?? 'minutes_per_day'
  const available = result.points.filter((point) => point.estimate != null)
  const unavailable = result.points.filter((point) => point.estimate == null)
  const measureLabel =
    MEASURE_SHORT_LABELS[result.measure] ?? result.measure.replaceAll('_', ' ')

  return (
    <div className="result-stack">
      <section className="card" aria-label="Trend">
        <p style={{ marginBottom: '0.4rem' }} className="field-hint">
          {available.length} yearly estimates
          {unavailable.length > 0
            ? `; ${unavailable.length === 1 ? `${unavailable[0]?.year} is` : `${unavailable.length} years are`} unavailable and shown as an explicit gap`
            : ''}
          . Hover or use arrow keys for each year's value.
        </p>
        <TrendChart points={result.points} unit={unit} measureLabel={measureLabel} />
      </section>

      <WarningBanner warnings={result.warnings} />

      <details className="panel" open>
        <summary>View data</summary>
        <div className="panel__body table-scroll">
          <table className="data-table">
            <caption className="visually-hidden">
              {measureLabel} by year ({unitLabel(unit)})
            </caption>
            <thead>
              <tr>
                <th scope="col">Year</th>
                <th scope="col" className="num">
                  Estimate
                </th>
                <th scope="col" className="num">
                  SE
                </th>
                <th scope="col">Confidence interval</th>
                <th scope="col" className="num">
                  Respondents
                </th>
              </tr>
            </thead>
            <tbody>
              {result.points.map((point) => (
                <tr key={point.year}>
                  <th scope="row">{point.year}</th>
                  {point.estimate == null ? (
                    <td colSpan={4}>
                      <i>Unavailable</i> — {point.unavailable_reason}
                    </td>
                  ) : (
                    <>
                      <td className="num">{formatValueShort(point.estimate.value, unit)}</td>
                      <td className="num">
                        {point.estimate.standard_error != null
                          ? formatStandardError(point.estimate.standard_error, unit)
                          : '—'}
                      </td>
                      <td>
                        {point.estimate.ci_lower != null && point.estimate.ci_upper != null
                          ? formatConfidenceInterval(
                              point.estimate.ci_lower,
                              point.estimate.ci_upper,
                              unit,
                            )
                          : '—'}
                      </td>
                      <td className="num">
                        {point.n_respondents != null ? formatCount(point.n_respondents) : '—'}
                      </td>
                    </>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>

      <MethodologyPanel rows={trendMethodologyRows(result)} analysisKey={analysisKey} />
    </div>
  )
}
