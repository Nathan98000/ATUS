/**
 * The small print under a result's chart: what the shading or thin lines
 * mean, any note about this chart (a missing year, what a link's labels
 * stand for), the API's context notes, and where the numbers come from,
 * with the graph's credit. Said once, in plain words.
 */
import type { ReactNode } from 'react'

import { confidenceIntervalName } from '../../utils/format'

interface ChartFootnoteProps {
  /** How the chart draws each estimate's confidence interval, if it does. */
  range?: { mark: 'band' | 'whisker'; level: number | null }
  notes?: readonly ReactNode[]
  /**
   * Context true of every result of its kind (`contextNotes`): always shown,
   * as one line, so a phone's small print stays shorter than its chart
   * (review round 4).
   */
  context?: readonly string[]
  /** Appended to the source line, e.g. "6,146 people's diaries". */
  sample?: string
}

export function ChartFootnote({ range, notes = [], context = [], sample }: ChartFootnoteProps) {
  return (
    <div className="chart-footnote">
      {range ? (
        <p className="chart-key">
          <span
            className={`chart-key__swatch chart-key__swatch--${range.mark}`}
            aria-hidden="true"
          />
          {range.mark === 'band' ? 'Shaded band' : 'Thin lines'}:{' '}
          {confidenceIntervalName(range.level)}. The survey interviews a sample of people, so
          each figure is an estimate subject to sampling error.
        </p>
      ) : null}
      {notes.map((note, index) => (
        <p key={index}>{note}</p>
      ))}
      {context.length > 0 ? <p>{context.join(' ')}</p> : null}
      <SourceLine sample={sample} />
    </div>
  )
}

/** Where the numbers come from and who made the graph, on one line. */
export function SourceLine({ sample, className }: { sample?: string; className?: string }) {
  return (
    <p className={className ?? 'chart-source'}>
      Source: American Time Use Survey, U.S. Bureau of Labor Statistics
      {sample ? ` · ${sample}` : ''}. Graph by Nathan Nguyen · X:{' '}
      <a href="https://x.com/NathanDNguyen" rel="noreferrer">
        @NathanDNguyen
      </a>
    </p>
  )
}
