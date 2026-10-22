/**
 * Comparison result: the answer in one sentence over a bar chart from zero,
 * then the API-computed difference (group A − group B, covariance-correct
 * SE — never recomputed here) in words, the chart's small print, and the
 * result's notes.
 */
import type { CompareResponse } from '../../api/types'
import { WarningBanner } from '../../components/WarningBanner'
import { contextNotes } from '../../domain/notes'
import { describeDifference } from '../../domain/describe'
import { formatCount } from '../../utils/format'
import { ChartFootnote } from './ChartFootnote'
import { ComparisonChart } from './ComparisonChart'

export function CompareView({
  result,
  note,
}: {
  result: CompareResponse
  /** What a link's own labels stand for (`definitionNote`), if anything. */
  note?: string | null
}) {
  const unit = result.group_a.estimate.unit
  const described = describeDifference(result)
  const hasIntervals = [result.group_a, result.group_b].some(
    (group) => group.estimate.ci_lower != null && group.estimate.ci_upper != null,
  )

  return (
    <div className="result-stack">
      <section className="card result-card" aria-label="Group estimates">
        <p className="result-lead">{described.sentence}</p>
        <ComparisonChart
          unit={unit}
          groups={[
            {
              label: result.label_a,
              estimate: result.group_a.estimate,
              colorVar: '--series-1',
              role: 'Group A',
            },
            {
              label: result.label_b,
              estimate: result.group_b.estimate,
              colorVar: '--series-2',
              role: 'Group B',
            },
          ]}
        />

        <div className="difference-block" data-testid="difference">
          <p className="difference-block__label">Difference</p>
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
          {described.interval !== null ? (
            <p className="difference-block__range">{described.interval}</p>
          ) : null}
        </div>

        <ChartFootnote
          range={
            hasIntervals
              ? { mark: 'whisker', level: result.group_a.estimate.confidence_level ?? null }
              : undefined
          }
          notes={[...(note != null ? [note] : []), ...contextNotes(result.warnings)]}
          sample={`${result.label_a}: ${formatCount(result.group_a.n_respondents)} diaries; ${result.label_b}: ${formatCount(result.group_b.n_respondents)} diaries`}
        />
      </section>

      <WarningBanner warnings={result.warnings} />
    </div>
  )
}
