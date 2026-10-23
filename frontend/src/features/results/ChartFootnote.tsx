/**
 * The small print under a result's chart: what the shading or thin lines
 * mean (with a link to what "95%" means), any note about this chart (a
 * missing year, what a link's labels stand for), the API's context notes,
 * how many diaries are behind the figures, and where the numbers come from,
 * with the graph's credit. Said once, in plain words.
 */
import type { ReactNode } from 'react'
import { Link } from 'react-router'

import { confidenceIntervalName, formatConfidenceLevel } from '../../utils/format'

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
  /**
   * How many diaries are behind the figures, e.g. "Based on 6,146 people's
   * diaries." Its own line: the source line reads exactly as the owner
   * wrote it (review round 6).
   */
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
          {/* The whiskers are each bar's own interval, not the difference's. */}
          {range.mark === 'whisker' ? "each group's " : ''}
          {confidenceIntervalName(range.level)}. The survey interviews a sample of people, so
          each figure is an estimate subject to sampling error.{' '}
          <IntervalHelp level={range.level} />
        </p>
      ) : null}
      {notes.map((note, index) => (
        <p key={index}>{note}</p>
      ))}
      {context.length > 0 ? <p>{context.join(' ')}</p> : null}
      {sample ? <p>{sample}</p> : null}
      <SourceLine />
    </div>
  )
}

/**
 * A link to the About page's plain explanation of the interval (review
 * round 6): "What does 95% mean?".
 */
export function IntervalHelp({ level }: { level: number | null | undefined }) {
  return (
    <Link to="/about#how-sure">
      {level != null
        ? `What does ${formatConfidenceLevel(level)} mean?`
        : 'What is a confidence interval?'}
    </Link>
  )
}

/**
 * Where the numbers come from and who made the graph, on one line, exactly
 * as the owner wrote it; "X: @NathanDNguyen" never breaks across lines.
 */
export function SourceLine({ className }: { className?: string }) {
  return (
    <p className={className ?? 'chart-source'}>
      Source: American Time Use Survey, U.S. Bureau of Labor Statistics. Graph by Nathan Nguyen
      ·{' '}
      <span className="chart-source__handle">
        X:{' '}
        <a href="https://x.com/NathanDNguyen" rel="noreferrer">
          @NathanDNguyen
        </a>
      </span>
    </p>
  )
}
