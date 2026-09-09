/**
 * Comparison dot-and-interval chart: two point estimates with confidence
 * intervals on one shared scale. Groups are distinguished by label and
 * position, never by color alone. All values come from the API response.
 */
import type { EstimateValue } from '../../api/types'
import {
  formatConfidenceInterval,
  formatStandardError,
  formatValueShort,
  unitLabel,
} from '../../utils/format'
import { linearScale, niceTicks, padDomain } from './chartScale'

const WIDTH = 720
const ROW_HEIGHT = 64
const MARGIN = { top: 12, right: 30, bottom: 34, left: 16 }

interface ComparisonChartProps {
  groups: { label: string; estimate: EstimateValue; colorVar: string }[]
  unit: string
}

export function ComparisonChart({ groups, unit }: ComparisonChartProps) {
  if (groups.length === 0) return null

  const lows = groups.map((group) => group.estimate.ci_lower ?? group.estimate.value)
  const highs = groups.map((group) => group.estimate.ci_upper ?? group.estimate.value)
  const [xMin, xMax] = padDomain(Math.min(...lows), Math.max(...highs), 0.15)
  const height = MARGIN.top + groups.length * ROW_HEIGHT + MARGIN.bottom
  const x = linearScale([xMin, xMax], [MARGIN.left, WIDTH - MARGIN.right])
  const ticks = niceTicks(xMin, xMax, 5)

  const summary = groups
    .map((group) => {
      let text = `${group.label}: ${formatValueShort(group.estimate.value, unit)}`
      if (group.estimate.ci_lower != null && group.estimate.ci_upper != null) {
        text += ` (CI ${formatConfidenceInterval(group.estimate.ci_lower, group.estimate.ci_upper, unit)})`
      }
      return text
    })
    .join('; ')

  return (
    <div>
      {/* SVG with role="img" + aria-label is the standard accessible-graphic
          pattern; the exact values are in the table below the chart. */}
      {/* oxlint-disable-next-line jsx-a11y/prefer-tag-over-role */}
      <svg
        role="img"
        viewBox={`0 0 ${WIDTH} ${height}`}
        className="chart-svg"
        aria-label={`Comparison of ${unitLabel(unit)}. ${summary}. Exact values are in the table below.`}
      >
        {ticks.map((tick) => (
          <g key={tick}>
            <line
              x1={x(tick)}
              x2={x(tick)}
              y1={MARGIN.top}
              y2={height - MARGIN.bottom}
              className="chart-grid"
            />
            <text x={x(tick)} y={height - MARGIN.bottom + 18} className="chart-tick">
              {unit === 'proportion_of_population'
                ? `${Math.round(tick * 100)}%`
                : tick.toLocaleString('en-US')}
            </text>
          </g>
        ))}
        {groups.map((group, groupIndex) => {
          const centerY = MARGIN.top + groupIndex * ROW_HEIGHT + ROW_HEIGHT / 2 + 8
          const { value, ci_lower, ci_upper, standard_error } = group.estimate
          return (
            <g key={group.label} data-testid={`compare-row-${groupIndex}`}>
              <text x={MARGIN.left} y={centerY - 20} className="chart-group-label">
                {group.label}
              </text>
              {ci_lower != null && ci_upper != null ? (
                <line
                  x1={x(ci_lower)}
                  x2={x(ci_upper)}
                  y1={centerY}
                  y2={centerY}
                  className="chart-ci-line"
                  style={{ stroke: `var(${group.colorVar})` }}
                  data-testid="ci-line"
                />
              ) : null}
              {ci_lower != null && ci_upper != null ? (
                <>
                  <line
                    x1={x(ci_lower)}
                    x2={x(ci_lower)}
                    y1={centerY - 5}
                    y2={centerY + 5}
                    className="chart-ci-cap"
                    style={{ stroke: `var(${group.colorVar})` }}
                  />
                  <line
                    x1={x(ci_upper)}
                    x2={x(ci_upper)}
                    y1={centerY - 5}
                    y2={centerY + 5}
                    className="chart-ci-cap"
                    style={{ stroke: `var(${group.colorVar})` }}
                  />
                </>
              ) : null}
              <circle
                cx={x(value)}
                cy={centerY}
                r={6}
                style={{ fill: `var(${group.colorVar})` }}
              />
              <text x={x(value)} y={centerY + 24} className="chart-value-label">
                {formatValueShort(value, unit)}
                {standard_error != null
                  ? ` ± ${formatStandardError(standard_error, unit)}`
                  : ''}
              </text>
            </g>
          )
        })}
      </svg>
      <p className="field-hint">
        Dots are the estimates; horizontal lines are their confidence intervals on the shared
        scale ({unitLabel(unit)}).
      </p>
    </div>
  )
}
