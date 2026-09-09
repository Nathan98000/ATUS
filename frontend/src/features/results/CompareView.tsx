/**
 * Comparison result: both group estimates, the API-computed difference
 * (group A − group B, covariance-correct SE — never recomputed here), a
 * dot-and-interval chart, warnings, and methodology.
 */
import type { CompareResponse } from '../../api/types'
import { WarningBanner } from '../../components/WarningBanner'
import {
  formatConfidenceInterval,
  formatConfidenceLevel,
  formatCount,
  formatDifferenceInterval,
  formatDifferenceValue,
  formatEstimate,
  formatStandardError,
  unitLabel,
} from '../../utils/format'
import { ComparisonChart } from './ComparisonChart'
import { estimateMethodologyRows } from './EstimateView'
import { MethodologyPanel } from './MethodologyPanel'

export function CompareView({
  result,
  analysisKey,
}: {
  result: CompareResponse
  analysisKey: string | null
}) {
  const unit = result.group_a.estimate.unit
  const difference = result.difference

  const methodologyRows = [
    {
      term: 'Groups',
      detail: `${result.label_a}: ${result.group_a.population}. ${result.label_b}: ${result.group_b.population}.`,
    },
    {
      term: 'Difference',
      detail: `Computed as ${result.label_a} − ${result.label_b}; its standard error uses per-replicate differences, which accounts for the correlation between the two groups' estimates.`,
    },
    ...estimateMethodologyRows(result.group_a).filter((row) => row.term !== 'Population'),
  ]

  return (
    <div className="result-stack">
      <section className="card" aria-label="Group estimates">
        <div className="compare-cards">
          {(
            [
              {
                label: result.label_a,
                group: result.group_a,
                colorVar: '--group-a',
                role: 'Group A',
              },
              {
                label: result.label_b,
                group: result.group_b,
                colorVar: '--group-b',
                role: 'Group B',
              },
            ] as const
          ).map(({ label, group, colorVar, role }) => {
            const formatted = formatEstimate(group.estimate.value, unit)
            return (
              <div
                key={role}
                className="compare-card"
                style={{ borderTopColor: `var(${colorVar})` }}
              >
                <p className="compare-card__role">{role}</p>
                <h3>{label}</h3>
                <p className="compare-card__value">{formatted.primary}</p>
                {formatted.secondary ? (
                  <p className="field-hint">{formatted.secondary}</p>
                ) : null}
                {group.estimate.standard_error != null ? (
                  <p className="field-hint">
                    ± {formatStandardError(group.estimate.standard_error, unit)}
                    {group.estimate.ci_lower != null && group.estimate.ci_upper != null
                      ? ` · CI ${formatConfidenceInterval(group.estimate.ci_lower, group.estimate.ci_upper, unit)}`
                      : ''}
                  </p>
                ) : null}
                <p className="field-hint">{formatCount(group.n_respondents)} respondents</p>
              </div>
            )
          })}
        </div>

        <ComparisonChart
          unit={unit}
          groups={[
            { label: result.label_a, estimate: result.group_a.estimate, colorVar: '--group-a' },
            { label: result.label_b, estimate: result.group_b.estimate, colorVar: '--group-b' },
          ]}
        />

        <div className="difference-block" data-testid="difference">
          <h3>
            Difference ({result.label_a} − {result.label_b})
          </h3>
          <p className="difference-block__value">
            {difference.value > 0 ? '+' : ''}
            {formatDifferenceValue(difference.value, unit)}
          </p>
          {difference.standard_error != null ? (
            <p className="field-hint">
              Standard error ± {formatStandardError(difference.standard_error, unit)}
              {difference.ci_lower != null &&
              difference.ci_upper != null &&
              difference.confidence_level != null
                ? ` · ${formatConfidenceLevel(difference.confidence_level)} CI ${formatDifferenceInterval(difference.ci_lower, difference.ci_upper, unit)}`
                : ''}
            </p>
          ) : null}
          <p className="field-hint">
            A positive difference means “{result.label_a}” has the larger estimate.
          </p>
        </div>
      </section>

      <WarningBanner warnings={result.warnings} />

      <details className="panel">
        <summary>View exact values</summary>
        <div className="panel__body table-scroll">
          <table className="data-table">
            <caption className="visually-hidden">
              Exact comparison values ({unitLabel(unit)})
            </caption>
            <thead>
              <tr>
                <th scope="col">Series</th>
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
              {(
                [
                  {
                    name: `${result.label_a} (Group A)`,
                    estimate: result.group_a.estimate,
                    n: result.group_a.n_respondents,
                  },
                  {
                    name: `${result.label_b} (Group B)`,
                    estimate: result.group_b.estimate,
                    n: result.group_b.n_respondents,
                  },
                  { name: `Difference (A − B)`, estimate: difference, n: null },
                ] as const
              ).map((row) => (
                <tr key={row.name}>
                  <th scope="row">{row.name}</th>
                  <td className="num">{row.estimate.value}</td>
                  <td className="num">{row.estimate.standard_error ?? '—'}</td>
                  <td>
                    {row.estimate.ci_lower != null && row.estimate.ci_upper != null
                      ? `${row.estimate.ci_lower} to ${row.estimate.ci_upper}`
                      : '—'}
                  </td>
                  <td className="num">{row.n ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>

      <MethodologyPanel rows={methodologyRows} analysisKey={analysisKey} />
    </div>
  )
}
