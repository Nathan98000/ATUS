/**
 * Year, statistic, and "More options" controls for the analysis builder.
 * Options are driven by GET /api/v1/meta (available years, measures,
 * weight schemes) — the UI never invents capabilities.
 */
import { useId, useState } from 'react'

import type { MeasureInfo, WeightSchemeInfo } from '../../api/types'
import { humanize, stripVariableJargon } from '../../domain/describe'
import { formatConfidenceLevel } from '../../utils/format'
import {
  weightsForSingleYear,
  KNOWN_MEASURES,
  KNOWN_WEIGHT_SCHEMES,
  type BuilderState,
  type Measure,
  type VarianceMethod,
  type WeightScheme,
} from './state'

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
 * When: a range for a trend, one year otherwise. Several years combined
 * (pooled) is turned on under "More options" (`MoreOptions`); once on, the
 * years to combine are chosen here, where the analysis's years are always
 * visible (review round 4).
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
              {ascending.map((year) => (
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
              {ascending.map((year) => (
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
            onChange={(event) => {
              const year = Number(event.target.value)
              onChange({ singleYear: year, ...weightsForSingleYear(year, state.weights) })
            }}
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
      {state.yearsMode === 'single' &&
      state.singleYear === 2020 &&
      state.weights === 'pandemic' ? (
        <p className="info-note" style={{ marginTop: '0.5rem' }}>
          2020 is a partial-year estimate: the survey paused that spring because of COVID-19, so
          it covers the rest of the year.
        </p>
      ) : null}
      {error ? <p className="error-text">{error}</p> : null}
    </div>
  )
}

/** Back to one year from several combined: the latest one chosen, with its weights. */
function singleYearFrom(
  state: BuilderState,
  availableYears: readonly number[],
): Partial<BuilderState> {
  const year = state.pooledYears.at(-1) ?? state.singleYear ?? availableYears.at(-1)
  if (year === undefined) return { yearsMode: 'single' }
  return { yearsMode: 'single', singleYear: year, ...weightsForSingleYear(year, state.weights) }
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

const CONFIDENCE_LEVELS = [0.9, 0.95, 0.99]

/**
 * The offered levels, plus the current one when a shared analysis carries
 * another level the API accepts (e.g. 0.975): without its own option the
 * select showed "90%" while Analyze sent 0.975, and choosing 90% fired no
 * change (review round 3).
 */
function confidenceLevelOptions(current: number): number[] {
  if (CONFIDENCE_LEVELS.includes(current)) return CONFIDENCE_LEVELS
  return [...CONFIDENCE_LEVELS, current].sort((a, b) => a - b)
}

/** Our own words for the weights this UI knows; another scheme keeps the API's. */
const WEIGHT_LABELS: Record<WeightScheme, { label: string; hint: string }> = {
  multiyear: {
    label: 'Standard weights (every year except 2020)',
    hint: 'Use these for nearly everything: they make the years comparable.',
  },
  pandemic: {
    label: '2020 partial-year weights (2019 and 2020 only)',
    hint: 'They cover only the weeks of 2020 when the survey ran: January 1 – March 17 and May 10 – December 31.',
  },
}

interface MoreOptionsProps {
  state: BuilderState
  weightSchemes: readonly WeightSchemeInfo[]
  varianceMethods: readonly string[]
  onChange: (update: Partial<BuilderState>) => void
}

/**
 * Choices few questions need: several years combined, the weights, and the
 * likely range. Open from the start whenever one is not at its default (a
 * shared link that sets one), so nothing in effect is hidden.
 */
export function MoreOptions({
  state,
  weightSchemes,
  varianceMethods,
  onChange,
}: MoreOptionsProps) {
  const id = useId()
  const [open, setOpen] = useState(
    () =>
      state.yearsMode === 'pooled' ||
      state.weights !== 'multiyear' ||
      state.variance !== 'replicate' ||
      state.confidenceLevel !== 0.95,
  )
  const knownSchemes = weightSchemes.filter((scheme) =>
    (KNOWN_WEIGHT_SCHEMES as readonly string[]).includes(scheme.name),
  )

  return (
    <details
      className="panel"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>More options</summary>
      <div className="panel__body">
        {state.operation !== 'trend' ? (
          <label className="radio-row">
            <input
              type="checkbox"
              checked={state.yearsMode === 'pooled'}
              onChange={(event) =>
                onChange(
                  event.target.checked
                    ? {
                        yearsMode: 'pooled',
                        pooledYears:
                          state.pooledYears.length > 0
                            ? state.pooledYears
                            : state.singleYear !== null
                              ? [state.singleYear]
                              : [],
                      }
                    : singleYearFrom(state, state.pooledYears),
                )
              }
            />
            <span className="radio-row__text">
              <b>Combine several years</b>
              <span className="field-hint" style={{ display: 'block' }}>
                One estimate from the diaries of several years — useful for a small group.
              </span>
            </span>
          </label>
        ) : null}

        <fieldset style={{ marginTop: state.operation !== 'trend' ? '0.9rem' : 0 }}>
          <legend>Weights</legend>
          {knownSchemes.map((scheme) => {
            const labels = WEIGHT_LABELS[scheme.name as WeightScheme]
            return (
              <label key={scheme.name} className="radio-row">
                <input
                  type="radio"
                  name={`${id}-weights`}
                  checked={state.weights === scheme.name}
                  onChange={() => onChange({ weights: scheme.name as WeightScheme })}
                />
                <span className="radio-row__text">
                  <b>{labels?.label ?? humanize(scheme.name)}</b>
                  <span className="field-hint" style={{ display: 'block' }}>
                    {labels?.hint ?? stripVariableJargon(scheme.description)}
                  </span>
                </span>
              </label>
            )
          })}
        </fieldset>

        <fieldset style={{ marginTop: '0.9rem' }}>
          <legend>Likely range</legend>
          {varianceMethods.includes('replicate') ? (
            <label className="radio-row">
              <input
                type="radio"
                name={`${id}-variance`}
                checked={state.variance === 'replicate'}
                onChange={() => onChange({ variance: 'replicate' })}
              />
              <span className="radio-row__text">
                <b>Show the likely range (recommended)</b>
                <span className="field-hint" style={{ display: 'block' }}>
                  From the survey's official replicate weights. Slower for long periods.
                </span>
              </span>
            </label>
          ) : null}
          {varianceMethods.includes('none') ? (
            <label className="radio-row">
              <input
                type="radio"
                name={`${id}-variance`}
                checked={state.variance === 'none'}
                onChange={() => onChange({ variance: 'none' })}
              />
              <span className="radio-row__text">
                <b>Don't show it</b>
                <span className="field-hint" style={{ display: 'block' }}>
                  Faster, but nothing says how precise each figure is.
                </span>
              </span>
            </label>
          ) : null}
        </fieldset>

        <div className="field" style={{ marginTop: '0.9rem', maxWidth: '14rem' }}>
          <label htmlFor={`${id}-confidence`}>Likely range certainty</label>
          <select
            id={`${id}-confidence`}
            value={state.confidenceLevel}
            onChange={(event) => onChange({ confidenceLevel: Number(event.target.value) })}
            disabled={state.variance === 'none'}
          >
            {confidenceLevelOptions(state.confidenceLevel).map((level) => (
              <option key={level} value={level}>
                {formatConfidenceLevel(level)}
              </option>
            ))}
          </select>
        </div>
      </div>
    </details>
  )
}

/** VarianceMethod is exported for completeness of the builder vocabulary. */
export type { VarianceMethod }
