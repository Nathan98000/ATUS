/**
 * Comparison result: the answer in one sentence, both group estimates, a bar
 * chart from zero, and the API-computed difference (group A − group B,
 * covariance-correct SE — never recomputed here) in words, then warnings
 * and methodology.
 */
import type { CompareResponse } from '../../api/types'
import { WarningBanner } from '../../components/WarningBanner'
import { describeDifference } from '../../domain/describe'
import {
  formatConfidenceInterval,
  formatCount,
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
  const described = describeDifference(result)

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
        <p className="result-lead">{described.sentence}</p>
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
            {
              label: result.label_a,
              estimate: result.group_a.estimate,
              colorVar: '--group-a',
              role: 'Group A',
            },
            {
              label: result.label_b,
              estimate: result.group_b.estimate,
              colorVar: '--group-b',
              role: 'Group B',
            },
          ]}
        />

        <div className="difference-block" data-testid="difference">
          <h3>Difference</h3>
          <p className="difference-block__value">
            {described.larger === null ? (
              described.amount
            ) : (
              <>
                {described.amount} for{' '}
                <span className="difference-block__group">
                  {described.larger === 'a' ? result.label_a : result.label_b}
                </span>
              </>
            )}
          </p>
          {described.interval !== null ? <p>{described.interval}</p> : null}
          <p className="field-hint">
            Exact difference ({result.label_a} − {result.label_b}):{' '}
            {difference.value > 0 ? '+' : ''}
            {formatDifferenceValue(difference.value, unit)}
            {difference.standard_error != null
              ? `, standard error ${formatStandardError(difference.standard_error, unit)}`
              : ''}
            .
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
                  Standard error
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

      <MethodologyPanel
        rows={methodologyRows}
        analysisKey={analysisKey}
        apiNotes={result.warnings}
      />
    </div>
  )
}
