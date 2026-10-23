/**
 * Comparison bar chart: each group's estimate as a bar from zero, with its
 * confidence interval as a whisker, on one shared scale. Bars start at zero
 * so their lengths compare honestly; a zoomed axis made small differences
 * look large. Built from HTML rather than a scaled SVG, so labels wrap and
 * text keeps its size on every screen. Groups are distinguished by label
 * and position, never by color alone; each value is labeled beside its bar,
 * and the chart's small print (ChartFootnote) explains the whiskers. All
 * values come from the API response.
 */
import type { CSSProperties } from 'react'

import type { EstimateValue } from '../../api/types'
import {
  confidenceIntervalName,
  formatAxisTicks,
  formatConfidenceInterval,
  formatDurationTick,
  formatEstimate,
  formatValueShort,
  unitLabel,
} from '../../utils/format'
import { durationTicksFromZero, ticksFromZero } from './chartScale'
import { useElementWidth } from './useElementWidth'

/** Room each axis label needs, so labels never collide on a narrow chart. */
const MIN_TICK_SPACING = 72

/**
 * The labels the chart draws. Two equal labels would leave only the colour
 * to tell the rows apart, so each is then prefixed with its group ("Group A:
 * …"), as the table below names them (review round 2).
 */
function chartLabels(groups: readonly { label: string; role?: string }[]): string[] {
  const labels = groups.map((group) => group.label)
  if (new Set(labels).size === labels.length) return labels
  return groups.map((group) => (group.role ? `${group.role}: ${group.label}` : group.label))
}

function isDuration(unit: string): boolean {
  return unit === 'minutes_per_day' || unit === 'minutes_per_day_of_participants'
}

interface ComparisonChartProps {
  /** `role` names the group ("Group A") where two drawn labels would otherwise read the same. */
  groups: { label: string; estimate: EstimateValue; colorVar: string; role?: string }[]
  unit: string
}

export function ComparisonChart({ groups, unit }: ComparisonChartProps) {
  const [wrapRef, width] = useElementWidth<HTMLDivElement>(720)
  if (groups.length === 0) return null

  // The bars' share of the chart: beside the labels on wide screens, below
  // them (full width, less the value column) on phones (see global.css).
  const trackWidth = width > 560 ? width - 300 : width - 80
  const intervals = Math.min(5, Math.max(2, Math.floor(trackWidth / MIN_TICK_SPACING)))
  const highest = Math.max(
    ...groups.map((group) => group.estimate.ci_upper ?? group.estimate.value),
  )
  const ticks = isDuration(unit)
    ? durationTicksFromZero(highest, intervals)
    : ticksFromZero(highest, Math.min(intervals, 4))
  const tickLabels = isDuration(unit)
    ? ticks.map(formatDurationTick)
    : formatAxisTicks(ticks, unit)
  const domainMax = ticks[ticks.length - 1] || 1
  const percent = (value: number) => `${(Math.max(0, value) / domainMax) * 100}%`
  const labels = chartLabels(groups)

  const summary = groups
    .map((group) => {
      let text = `${group.label}: ${formatValueShort(group.estimate.value, unit)}`
      if (group.estimate.ci_lower != null && group.estimate.ci_upper != null) {
        text += ` (${confidenceIntervalName(group.estimate.confidence_level)} ${formatConfidenceInterval(group.estimate.ci_lower, group.estimate.ci_upper, unit)})`
      }
      return text
    })
    .join('; ')

  const gridlines = ticks.map((tick) => (
    <span key={tick} className="compare-chart__gridline" style={{ left: percent(tick) }} />
  ))

  return (
    <div className="compare-chart-wrap" ref={wrapRef}>
      {/* role="img" + aria-label is the standard accessible-graphic pattern;
          the label carries every value the bars show. */}
      <div
        // oxlint-disable-next-line jsx-a11y/prefer-tag-over-role
        role="img"
        className="compare-chart"
        aria-label={`Comparison of ${unitLabel(unit)}. ${summary}.`}
      >
        {groups.map((group, groupIndex) => {
          const { value, ci_lower, ci_upper } = group.estimate
          const color = { '--bar-color': `var(${group.colorVar})` } as CSSProperties
          return (
            <div
              key={groupIndex}
              className="compare-chart__row"
              data-testid={`compare-row-${groupIndex}`}
              style={color}
            >
              <span className="compare-chart__label">{labels[groupIndex]}</span>
              <span className="compare-chart__track">
                {gridlines}
                <span className="compare-chart__bar" style={{ width: percent(value) }} />
                {ci_lower != null && ci_upper != null ? (
                  <span
                    className="compare-chart__ci"
                    data-testid="ci-line"
                    style={{
                      left: percent(ci_lower),
                      width: `calc(${percent(ci_upper)} - ${percent(ci_lower)})`,
                    }}
                  />
                ) : null}
              </span>
              <span className="compare-chart__value">
                {formatEstimate(value, unit).primary}
              </span>
            </div>
          )
        })}
        <div className="compare-chart__axis" aria-hidden="true">
          <span className="compare-chart__track compare-chart__track--axis">
            {ticks.map((tick, tickIndex) => (
              <span
                key={tick}
                className="compare-chart__tick"
                style={{ left: percent(tick) }}
                data-edge={
                  tickIndex === 0 ? 'start' : tickIndex === ticks.length - 1 ? 'end' : undefined
                }
              >
                {tickLabels[tickIndex]}
              </span>
            ))}
          </span>
        </div>
      </div>
    </div>
  )
}
