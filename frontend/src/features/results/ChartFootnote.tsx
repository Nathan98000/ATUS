/**
 * The small print under a result's chart: what the shading or thin lines
 * mean, any note about this chart (a missing year, what a link's labels
 * stand for), and where the numbers come from. Said once, in plain words.
 */
import type { ReactNode } from 'react'

import { formatConfidenceLevel } from '../../utils/format'

interface ChartFootnoteProps {
  /** How the chart draws each estimate's confidence interval, if it does. */
  range?: { mark: 'band' | 'whisker'; level: number | null }
  notes?: readonly ReactNode[]
  /** Appended to the source line, e.g. "6,146 people's diaries". */
  sample?: string
}

export function ChartFootnote({ range, notes = [], sample }: ChartFootnoteProps) {
  return (
    <div className="chart-footnote">
      {range ? (
        <p className="chart-key">
          <span
            className={`chart-key__swatch chart-key__swatch--${range.mark}`}
            aria-hidden="true"
          />
          {range.mark === 'band' ? 'Shaded band' : 'Thin lines'}: likely range
          {range.level != null ? ` (${formatConfidenceLevel(range.level)} confidence)` : ''}.
          The survey asks a sample of people, so each figure is an estimate.
        </p>
      ) : null}
      {notes.map((note, index) => (
        <p key={index}>{note}</p>
      ))}
      <p className="chart-source">
        Source: American Time Use Survey, U.S. Bureau of Labor Statistics
        {sample ? ` · ${sample}` : ''}.
      </p>
    </div>
  )
}
