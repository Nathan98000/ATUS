/**
 * Year and statistic controls for the analysis builder. Options are driven
 * by GET /api/v1/meta (available years, measures) — the UI never invents
 * capabilities. The weights and the confidence interval are not choices:
 * the site sets them (`analysisSettings`, review round 5).
 */
import { useId } from 'react'

import type { MeasureInfo } from '../../api/types'
import { humanize } from '../../domain/describe'
import { KNOWN_MEASURES, PARTIAL_YEAR, type BuilderState, type Measure } from './state'

/** Short human labels for the statistics this UI knows (presentation only). */
const MEASURE_LABELS: Record<Measure, { label: string; hint: string }> = {
  average_minutes_per_day: {
    label: 'Average time per day',
    hint: 'Across everyone in the population, including people who did not do the activity that day.',
  },
  participation_rate: {
    label: 'Share of people who do it',
    hint: 'The percentage of people who do the activity on an average day.',
  },
  average_minutes_per_participant: {
    label: 'Time among those who do it',
    hint: 'Average time among only the people who did the activity that day.',
  },
  participants_per_day: {
    label: 'Number of people who do it',
    hint: 'How many people do the activity on an average day.',
  },
}

interface YearSelectorProps {
  state: BuilderState
  availableYears: readonly number[]
  error?: string
  onChange: (update: Partial<BuilderState>) => void
}

/**
 * When: a range for a trend, one year otherwise. "From" offers the years up
 * to "To" and "To" the years from "From" on, so the range cannot run
 * backwards (review round 6). Several years combined
 * (pooled) comes only from a shared link, and shows here with its years
 * and a way back to one year, so nothing in effect is hidden.
 */
export function YearSelector({ state, availableYears, error, onChange }: YearSelectorProps) {
  const id = useId()
  const descending = [...availableYears].sort((a, b) => b - a)
  const ascending = [...availableYears].sort((a, b) => a - b)

  if (state.operation === 'trend') {
    return (
      <div>
        <div className="year-range">
          <div className="field">
            <label htmlFor={`${id}-from`}>From</label>
            <select
              id={`${id}-from`}
              value={state.trendFrom ?? ''}
              onChange={(event) => onChange({ trendFrom: Number(event.target.value) })}
            >
              {ascending
                .filter((year) => state.trendTo == null || year <= state.trendTo)
                .map((year) => (
                  <option key={year} value={year}>
                    {year}
                  </option>
                ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor={`${id}-to`}>To</label>
            <select
              id={`${id}-to`}
              value={state.trendTo ?? ''}
              onChange={(event) => onChange({ trendTo: Number(event.target.value) })}
            >
              {ascending
                .filter((year) => state.trendFrom == null || year >= state.trendFrom)
                .map((year) => (
                  <option key={year} value={year}>
                    {year}
                  </option>
                ))}
            </select>
          </div>
        </div>
        {error ? <p className="error-text">{error}</p> : null}
      </div>
    )
  }

  return (
    <div>
      {state.yearsMode === 'single' ? (
        <div className="field" style={{ maxWidth: '10rem' }}>
          <label htmlFor={`${id}-year`}>Year</label>
          <select
            id={`${id}-year`}
            value={state.singleYear ?? ''}
            onChange={(event) => onChange({ singleYear: Number(event.target.value) })}
          >
            {descending.map((year) => (
              <option key={year} value={year}>
                {year}
              </option>
            ))}
          </select>
        </div>
      ) : (
        <fieldset>
          <legend>Years to combine</legend>
          <div className="year-checkboxes">
            {ascending.map((year) => (
              <label key={year} className="year-checkbox">
                <input
                  type="checkbox"
                  checked={state.pooledYears.includes(year)}
                  onChange={(event) =>
                    onChange({
                      pooledYears: event.target.checked
                        ? [...state.pooledYears, year].sort((a, b) => a - b)
                        : state.pooledYears.filter((candidate) => candidate !== year),
                    })
                  }
                />
                <span>{year}</span>
              </label>
            ))}
          </div>
          <p className="field-hint">
            Their diaries are combined into one estimate for the whole period.{' '}
            <button
              type="button"
              className="link-button"
              onClick={() => onChange(singleYearFrom(state, availableYears))}
            >
              Use one year instead
            </button>
          </p>
        </fieldset>
      )}
      {state.yearsMode === 'single' && state.singleYear === PARTIAL_YEAR ? (
        <p className="info-note" style={{ marginTop: '0.5rem' }}>
          2020 is a partial-year estimate: the survey paused that spring because of COVID-19, so
          it covers the rest of the year.
        </p>
      ) : null}
      {error ? <p className="error-text">{error}</p> : null}
    </div>
  )
}

/** Back to one year from several combined: the latest one chosen. */
function singleYearFrom(
  state: BuilderState,
  availableYears: readonly number[],
): Partial<BuilderState> {
  const year = state.pooledYears.at(-1) ?? state.singleYear ?? availableYears.at(-1)
  if (year === undefined) return { yearsMode: 'single' }
  return { yearsMode: 'single', singleYear: year }
}

interface MeasureSelectorProps {
  measures: readonly MeasureInfo[]
  value: Measure
  onChange: (measure: Measure) => void
}

/**
 * What the chart shows: one select under the activity, with a line saying
 * what the chosen statistic means (review round 4: four radios with a hint
 * each made it the builder's tallest card, though most questions keep the
 * default).
 */
export function MeasureSelector({ measures, value, onChange }: MeasureSelectorProps) {
  const id = useId()
  const known = measures.filter((measure): measure is MeasureInfo & { name: Measure } =>
    (KNOWN_MEASURES as readonly string[]).includes(measure.name),
  )
  const chosen = known.find((measure) => measure.name === value)
  return (
    <div className="field measure-select">
      <label htmlFor={`${id}-measure`}>Show</label>
      <select
        id={`${id}-measure`}
        value={value}
        aria-describedby={`${id}-measure-hint`}
        onChange={(event) => onChange(event.target.value as Measure)}
      >
        {known.map((measure) => (
          <option key={measure.name} value={measure.name}>
            {MEASURE_LABELS[measure.name]?.label ?? humanize(measure.name)}
          </option>
        ))}
      </select>
      <p className="field-hint" id={`${id}-measure-hint`}>
        {chosen ? (MEASURE_LABELS[chosen.name]?.hint ?? chosen.description) : ''}
      </p>
    </div>
  )
}
