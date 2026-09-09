/**
 * Point-estimate result: the estimate with its uncertainty front and center,
 * the unweighted sample clearly distinguished from the weighted population,
 * API warnings always visible, and a methodology panel + exact-value table.
 */
import type { EstimateResponse } from '../../api/types'
import { WarningBanner } from '../../components/WarningBanner'
import { interpretEstimate } from '../../domain/describe'
import {
  formatConfidenceInterval,
  formatConfidenceLevel,
  formatCount,
  formatEstimate,
  formatPeople,
  formatStandardError,
  formatYears,
  unitLabel,
} from '../../utils/format'
import { MethodologyPanel, type MethodologyRow } from './MethodologyPanel'

export function estimateMethodologyRows(result: EstimateResponse): MethodologyRow[] {
  return [
    { term: 'Activity', detail: activityDetail(result) },
    { term: 'Population', detail: result.population },
    { term: 'Statistic', detail: result.measure.replaceAll('_', ' ') },
    { term: 'Years', detail: formatYears(result.years) },
    {
      term: 'Weighting',
      detail: `${result.weight.bls_variable} (${result.weight.scheme}) — official BLS survey weights; estimates represent person-days of the civilian noninstitutional population.`,
    },
    {
      term: 'Uncertainty',
      detail:
        result.variance_method === 'replicate'
          ? 'Standard errors from the official 160 replicate weights (successive-difference replication).'
          : 'None requested (point estimate only).',
    },
    ...(result.estimate.confidence_level != null
      ? [
          {
            term: 'Confidence interval',
            detail: `${formatConfidenceLevel(result.estimate.confidence_level)} normal approximation (estimate ± z × SE).`,
          },
        ]
      : []),
    { term: 'Days represented', detail: formatCount(result.days_in_period) },
    { term: 'Analytics version', detail: result.analytics_version },
  ]
}

function activityDetail(result: EstimateResponse): string {
  const { label, include, exclude, leaf_code_count } = result.activity
  let detail = `${label} — lexicon codes ${include.join(', ')}`
  if (exclude.length > 0) detail += ` excluding ${exclude.join(', ')}`
  detail += ` (${leaf_code_count} six-digit activities)`
  return detail
}

export function SampleLines({ result }: { result: EstimateResponse }) {
  return (
    <div className="sample-lines">
      <p>
        <b>Survey sample:</b> {formatCount(result.n_respondents)} respondents
        {result.measure !== 'average_minutes_per_day'
          ? ` (${formatCount(result.n_participants)} reported the activity)`
          : ` (${formatCount(result.n_participants)} reported this activity that day)`}
      </p>
      <p>
        <b>Represents:</b> {formatPeople(result.weighted_population_per_day)} people on an
        average day
      </p>
    </div>
  )
}

export function EstimateView({
  result,
  analysisKey,
}: {
  result: EstimateResponse
  analysisKey: string | null
}) {
  const { value, unit, standard_error, ci_lower, ci_upper, confidence_level } = result.estimate
  const formatted = formatEstimate(value, unit)
  const interpretation = interpretEstimate(result)

  return (
    <div className="result-stack">
      <section className="card" aria-label="Estimate">
        <div className="stat-block">
          <span className="stat-value">{formatted.primary}</span>
          {formatted.secondary ? (
            <span className="stat-secondary">{formatted.secondary}</span>
          ) : null}
          {standard_error != null ? (
            <p className="stat-uncertainty">
              Standard error <b>± {formatStandardError(standard_error, unit)}</b>
              {ci_lower != null && ci_upper != null && confidence_level != null ? (
                <>
                  {' · '}
                  {formatConfidenceLevel(confidence_level)} confidence interval{' '}
                  <b>{formatConfidenceInterval(ci_lower, ci_upper, unit)}</b>
                </>
              ) : null}
            </p>
          ) : (
            <p className="stat-uncertainty">
              No uncertainty was requested for this estimate (point estimate only).
            </p>
          )}
        </div>
        {interpretation ? (
          <p style={{ marginTop: '0.9rem', marginBottom: 0 }}>{interpretation}</p>
        ) : null}
        <SampleLines result={result} />
      </section>

      <WarningBanner warnings={result.warnings} />
      <MethodologyPanel rows={estimateMethodologyRows(result)} analysisKey={analysisKey} />

      <details className="panel">
        <summary>View exact values</summary>
        <div className="panel__body table-scroll">
          <table className="data-table">
            <caption className="visually-hidden">Exact values for this estimate</caption>
            <tbody>
              <tr>
                <th scope="row">Estimate ({unitLabel(unit)})</th>
                <td className="num">{value}</td>
              </tr>
              {standard_error != null ? (
                <tr>
                  <th scope="row">Standard error</th>
                  <td className="num">{standard_error}</td>
                </tr>
              ) : null}
              {ci_lower != null && ci_upper != null ? (
                <tr>
                  <th scope="row">Confidence interval</th>
                  <td className="num">
                    {ci_lower} to {ci_upper}
                  </td>
                </tr>
              ) : null}
              <tr>
                <th scope="row">Respondents (unweighted)</th>
                <td className="num">{result.n_respondents}</td>
              </tr>
              <tr>
                <th scope="row">Participants (unweighted)</th>
                <td className="num">{result.n_participants}</td>
              </tr>
              <tr>
                <th scope="row">Weighted population per day</th>
                <td className="num">{result.weighted_population_per_day}</td>
              </tr>
              <tr>
                <th scope="row">Days in period</th>
                <td className="num">{result.days_in_period}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </details>
    </div>
  )
}
