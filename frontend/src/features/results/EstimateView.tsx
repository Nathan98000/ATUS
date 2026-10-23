/**
 * Point-estimate result: the answer in one sentence, the number itself with
 * its 95% confidence interval, and a figure that puts it in proportion — the share of
 * a 24-hour day for a duration, a hundred people for a rate, the whole
 * population for a count — then the small print and the result's notes.
 */
import type { CSSProperties } from 'react'

import type { EstimateResponse } from '../../api/types'
import { WarningBanner } from '../../components/WarningBanner'
import { contextNotes } from '../../domain/notes'
import { interpretEstimate } from '../../domain/describe'
import {
  confidenceIntervalName,
  formatCount,
  formatEstimate,
  formatPeople,
  formatReadableInterval,
} from '../../utils/format'
import { ChartFootnote } from './ChartFootnote'

const MINUTES_PER_DAY = 24 * 60

/** What the big number counts, under it. */
function valueCaption(result: EstimateResponse): string {
  switch (result.estimate.unit) {
    case 'minutes_per_day':
      return 'per day, on average'
    case 'minutes_per_day_of_participants':
      return 'per day, among people who did it'
    default:
      return formatEstimate(result.estimate.value, result.estimate.unit).secondary ?? ''
  }
}

export function EstimateView({
  result,
  note,
}: {
  result: EstimateResponse
  /** What a link's own labels stand for (`definitionNote`), if anything. */
  note?: string | null
}) {
  const { value, unit, ci_lower, ci_upper, confidence_level } = result.estimate
  const formatted = formatEstimate(value, unit)
  const interpretation = interpretEstimate(result)

  return (
    <div className="result-stack">
      <section className="card result-card" aria-label="Estimate">
        {interpretation ? <p className="result-lead">{interpretation}</p> : null}
        <div className="estimate-hero">
          <div className="stat-block">
            <span className="stat-value">{formatted.primary}</span>
            <span className="stat-secondary">{valueCaption(result)}</span>
            {ci_lower != null && ci_upper != null ? (
              <p className="stat-uncertainty">
                {confidenceIntervalName(confidence_level, true)}:{' '}
                <b>{formatReadableInterval(ci_lower, ci_upper, unit)}</b>
              </p>
            ) : (
              <p className="stat-uncertainty">
                No confidence interval is available for this estimate.
              </p>
            )}
          </div>
          <EstimateFigure result={result} />
        </div>
        <ChartFootnote
          notes={note != null ? [note] : []}
          context={contextNotes(result.warnings)}
          sample={`${formatCount(result.n_respondents)} people's diaries`}
        />
      </section>

      <WarningBanner warnings={result.warnings} />
    </div>
  )
}

function EstimateFigure({ result }: { result: EstimateResponse }) {
  const { value, unit } = result.estimate
  if (unit === 'minutes_per_day' || unit === 'minutes_per_day_of_participants') {
    return <DayFigure minutes={value} />
  }
  if (unit === 'proportion_of_population') return <HundredFigure share={value} />
  if (unit === 'persons_per_day' && result.weighted_population_per_day > 0) {
    return <PeopleFigure people={value} population={result.weighted_population_per_day} />
  }
  return null
}

const HOUR_MARKS = [0, 6, 12, 18, 24]

/** The estimate as a stretch of one 24-hour day. */
function DayFigure({ minutes }: { minutes: number }) {
  const share = Math.min(1, Math.max(0, minutes / MINUTES_PER_DAY))
  const shown = formatEstimate(minutes, 'minutes_per_day').primary
  return (
    // oxlint-disable-next-line jsx-a11y/prefer-tag-over-role
    <div className="estimate-figure" role="img" aria-label={`${shown} out of 24 hours`}>
      <p className="estimate-figure__caption">Out of a 24-hour day</p>
      <div className="day-bar">
        <span
          className="day-bar__fill"
          data-testid="day-fill"
          style={{ '--share': share } as CSSProperties}
        />
      </div>
      <div className="day-bar__hours" aria-hidden="true">
        {HOUR_MARKS.map((hour) => (
          <span key={hour} style={{ left: `${(hour / 24) * 100}%` }}>
            {hour}h
          </span>
        ))}
      </div>
    </div>
  )
}

/** A rate as filled dots among a hundred people. */
function HundredFigure({ share }: { share: number }) {
  const filled = Math.min(100, Math.max(0, Math.round(share * 100)))
  const caption =
    share > 0 && filled === 0
      ? 'Fewer than 1 in every 100 people'
      : share < 1 && filled === 100
        ? 'Nearly everyone'
        : `About ${filled} in every 100 people`
  return (
    // oxlint-disable-next-line jsx-a11y/prefer-tag-over-role
    <div className="estimate-figure" role="img" aria-label={caption}>
      <p className="estimate-figure__caption">{caption}</p>
      <div className="hundred-grid" aria-hidden="true">
        {Array.from({ length: 100 }, (_, index) => (
          <span
            key={index}
            className={index < filled ? 'hundred-grid__dot is-filled' : 'hundred-grid__dot'}
          />
        ))}
      </div>
    </div>
  )
}

/** A count of people against the whole population it is drawn from. */
function PeopleFigure({ people, population }: { people: number; population: number }) {
  const share = Math.min(1, Math.max(0, people / population))
  const caption = `${formatPeople(people)} of ${formatPeople(population)} people`
  return (
    // oxlint-disable-next-line jsx-a11y/prefer-tag-over-role
    <div className="estimate-figure" role="img" aria-label={caption}>
      <p className="estimate-figure__caption">{caption}</p>
      <div className="day-bar">
        <span className="day-bar__fill" style={{ '--share': share } as CSSProperties} />
      </div>
    </div>
  )
}
