/**
 * Comparison result: the answer in one sentence over a bar chart from zero,
 * then the API-computed difference (group A − group B, covariance-correct
 * SE — never recomputed here) and its confidence interval in one short data
 * line, the result's notes, and the chart's small print.
 */
import type { CompareRequest, CompareResponse } from '../../api/types'
import { WarningBanner } from '../../components/WarningBanner'
import { contextNotes, fewDiariesNote } from '../../domain/notes'
import { describeDifference } from '../../domain/describe'
import { formatCount } from '../../utils/format'
import { ChartFootnote } from './ChartFootnote'
import { ComparisonChart } from './ComparisonChart'
import { FewDiaries } from './FewDiaries'

export function CompareView({
  result,
  spec,
  note,
  combine,
}: {
  result: CompareResponse
  /** The request, which tells built-in group names from a link's own. */
  spec?: CompareRequest
  /** What a link's own labels stand for (`definitionNote`), if anything. */
  note?: string | null
  /** The same comparison over the latest years combined (`combineYearsLink`). */
  combine?: { path: string; years: string } | null
}) {
  const unit = result.group_a.estimate.unit
  const described = describeDifference(result, spec)
  const hasIntervals = [result.group_a, result.group_b].some(
    (group) => group.estimate.ci_lower != null && group.estimate.ci_upper != null,
  )
  const few = [
    fewDiariesNote(result.group_a.n_respondents, result.group_a.n_participants, result.label_a),
    fewDiariesNote(result.group_b.n_respondents, result.group_b.n_participants, result.label_b),
  ].filter((text): text is string => text !== null)

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

        {/* One data line: the difference from the larger group's side, its
            interval in parentheses (review round 6: it restated the answer
            and named each group again). */}
        <p className="difference-line" data-testid="difference">
          <b>Difference:</b> {described.amount}
          {described.interval !== null ? ` (${described.interval})` : ''}
        </p>

        <WarningBanner
          warnings={result.warnings}
          notes={few.map((text, index) => (
            <FewDiaries
              key={index}
              text={text}
              combine={index === few.length - 1 ? combine : null}
            />
          ))}
        />
        <ChartFootnote
          range={
            hasIntervals
              ? { mark: 'whisker', level: result.group_a.estimate.confidence_level ?? null }
              : undefined
          }
          notes={note != null ? [note] : []}
          context={contextNotes(result.warnings)}
          sample={`Diaries behind each bar: ${result.label_a} ${formatCount(result.group_a.n_respondents)} · ${result.label_b} ${formatCount(result.group_b.n_respondents)}.`}
        />
      </section>
    </div>
  )
}
