/**
 * The answer to a home-page starter question, on its card: the number
 * itself, a trend's shape, or two groups side by side. It is read from the
 * deployed static snapshot only (`useSnapshotAnalysis`), so the home page
 * never wakes the analysis service; without a snapshot (development) or
 * until it loads, the card shows its question alone.
 */
import type { CSSProperties } from 'react'

import { useSnapshotAnalysis } from '../../api/queries'
import type {
  AnalysisRequest,
  CompareResponse,
  EstimateResponse,
  Operation,
  TrendResponse,
} from '../../api/types'
import { formatEstimate } from '../../utils/format'
import { availableSegments } from '../results/TrendChart'

export function StarterPreview({
  operation,
  spec,
}: {
  operation: Operation
  spec: AnalysisRequest
}) {
  const query = useSnapshotAnalysis(operation, spec)
  const result = query.data
  if (result == null) return null
  if (operation === 'estimate') return <EstimatePreview result={result as EstimateResponse} />
  if (operation === 'trend') return <TrendPreview result={result as TrendResponse} />
  return <ComparePreview result={result as CompareResponse} />
}

function perDay(unit: string): string {
  return unit === 'minutes_per_day' || unit === 'minutes_per_day_of_participants'
    ? ' a day'
    : ''
}

const MINUTES_PER_DAY = 24 * 60

function EstimatePreview({ result }: { result: EstimateResponse }) {
  const { value, unit } = result.estimate
  return (
    <span className="starter-preview starter-preview--estimate">
      {unit === 'minutes_per_day' ? (
        // The estimate against the whole 24-hour day it is part of.
        <span className="starter-preview__day" aria-hidden="true">
          <span
            style={{ width: `${Math.min(1, Math.max(0, value / MINUTES_PER_DAY)) * 100}%` }}
          />
        </span>
      ) : null}
      <span>
        <span className="starter-preview__value">{formatEstimate(value, unit).primary}</span>
        <span className="starter-preview__caption">
          {perDay(unit)} in {result.years.join(', ')}
        </span>
      </span>
    </span>
  )
}

/** Sparkline drawing box; stretched to the card's width. */
const SPARK_WIDTH = 100
const SPARK_HEIGHT = 28

function TrendPreview({ result }: { result: TrendResponse }) {
  const segments = availableSegments(result.points)
  const available = segments.flat()
  const last = available[available.length - 1]
  if (last === undefined) return null
  const unit =
    result.points.find((point) => point.estimate != null)?.estimate?.unit ?? 'minutes_per_day'
  const years = result.points.map((point) => point.year)
  const firstYear = Math.min(...years)
  const yearSpan = Math.max(...years) - firstYear || 1
  const values = available.map((point) => point.value)
  const low = Math.min(...values)
  const valueSpan = Math.max(...values) - low || 1
  const x = (year: number) => ((year - firstYear) / yearSpan) * SPARK_WIDTH
  const y = (value: number) =>
    SPARK_HEIGHT - 2 - ((value - low) / valueSpan) * (SPARK_HEIGHT - 4)

  return (
    <span className="starter-preview starter-preview--trend">
      <svg
        className="starter-preview__spark"
        viewBox={`0 0 ${SPARK_WIDTH} ${SPARK_HEIGHT}`}
        preserveAspectRatio="none"
        aria-hidden="true"
      >
        {segments.map((segment, index) => (
          <polyline
            key={index}
            points={segment.map((point) => `${x(point.year)},${y(point.value)}`).join(' ')}
          />
        ))}
      </svg>
      <span>
        <span className="starter-preview__value">
          {formatEstimate(last.value, unit).primary}
        </span>
        <span className="starter-preview__caption">
          {perDay(unit)} in {last.year}
        </span>
      </span>
    </span>
  )
}

function ComparePreview({ result }: { result: CompareResponse }) {
  const unit = result.group_a.estimate.unit
  const groups = [
    { label: result.label_a, value: result.group_a.estimate.value, color: '--group-a' },
    { label: result.label_b, value: result.group_b.estimate.value, color: '--group-b' },
  ]
  const highest = Math.max(...groups.map((group) => group.value)) || 1
  return (
    <span className="starter-preview starter-preview--compare">
      {groups.map((group) => (
        <span
          key={group.label}
          className="starter-preview__group"
          style={{ '--bar-color': `var(${group.color})` } as CSSProperties}
        >
          <span className="starter-preview__group-label">{group.label}</span>
          <span className="starter-preview__bar" aria-hidden="true">
            <span style={{ width: `${(Math.max(0, group.value) / highest) * 100}%` }} />
          </span>
          <span className="starter-preview__group-value">
            {formatEstimate(group.value, unit).primary}
          </span>
        </span>
      ))}
    </span>
  )
}
