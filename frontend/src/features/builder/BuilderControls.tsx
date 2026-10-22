/**
 * Year, statistic, and advanced-methodology controls for the analysis
 * builder. Options are driven by GET /api/v1/meta (available years, measures,
 * weight schemes) — the UI never invents capabilities.
 */
import { useId } from 'react'

import type { MeasureInfo, WeightSchemeInfo } from '../../api/types'
import { humanize } from '../../domain/describe'
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
        <p className="field-hint">One point per year, drawn as a line.</p>
        {state.weights === 'multiyear' &&
        state.trendFrom !== null &&
        state.trendTo !== null &&
        state.trendFrom <= 2020 &&
        state.trendTo >= 2020 &&
        availableYears.includes(2020) ? (
          <p className="info-note" style={{ marginTop: '0.5rem' }}>
            2020 is a partial-year estimate (a hollow point): the survey paused that spring
            because of COVID-19.
          </p>
        ) : null}
      </div>
    )
  }

  return (
    <div>
      <div className="segmented" role="radiogroup" aria-label="Year selection mode">
        <label>
          <input
            type="radio"
            name={`${id}-mode`}
            checked={state.yearsMode === 'single'}
            onChange={() => onChange({ yearsMode: 'single' })}
          />
          <span>One year</span>
        </label>
        <label>
          <input
            type="radio"
            name={`${id}-mode`}
            checked={state.yearsMode === 'pooled'}
            onChange={() =>
              onChange({
                yearsMode: 'pooled',
                pooledYears:
                  state.pooledYears.length > 0
                    ? state.pooledYears
                    : state.singleYear !== null
                      ? [state.singleYear]
                      : [],
              })
            }
          />
          <span>Several years combined</span>
        </label>
      </div>
      {state.yearsMode === 'single' ? (
        <div className="field" style={{ marginTop: '0.6rem', maxWidth: '10rem' }}>
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
        <fieldset style={{ marginTop: '0.6rem' }}>
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
            Their diaries are combined into one estimate for the whole period.
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

interface MeasureSelectorProps {
  measures: readonly MeasureInfo[]
  value: Measure
  onChange: (measure: Measure) => void
}

export function MeasureSelector({ measures, value, onChange }: MeasureSelectorProps) {
  const id = useId()
  const known = measures.filter((measure): measure is MeasureInfo & { name: Measure } =>
    (KNOWN_MEASURES as readonly string[]).includes(measure.name),
  )
  return (
    <div role="radiogroup" aria-label="Statistic">
      {known.map((measure) => {
        const labels = MEASURE_LABELS[measure.name]
        return (
          <label key={measure.name} className="radio-row">
            <input
              type="radio"
              name={`${id}-measure`}
              checked={value === measure.name}
              onChange={() => onChange(measure.name)}
            />
            <span className="radio-row__text">
              <b>{labels?.label ?? humanize(measure.name)}</b>
              <span className="field-hint" style={{ display: 'block' }}>
                {labels?.hint ?? measure.description}
              </span>
            </span>
          </label>
        )
      })}
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

interface AdvancedOptionsProps {
  state: BuilderState
  weightSchemes: readonly WeightSchemeInfo[]
  varianceMethods: readonly string[]
  onChange: (update: Partial<BuilderState>) => void
}

export function AdvancedOptions({
  state,
  weightSchemes,
  varianceMethods,
  onChange,
}: AdvancedOptionsProps) {
  const id = useId()
  const knownSchemes = weightSchemes.filter((scheme) =>
    (KNOWN_WEIGHT_SCHEMES as readonly string[]).includes(scheme.name),
  )

  return (
    <details className="panel">
      <summary>Advanced options</summary>
      <div className="panel__body">
        <fieldset>
          <legend>Weighting scheme</legend>
          {knownSchemes.map((scheme) => (
            <label key={scheme.name} className="radio-row">
              <input
                type="radio"
                name={`${id}-weights`}
                checked={state.weights === scheme.name}
                onChange={() => onChange({ weights: scheme.name as WeightScheme })}
              />
              <span className="radio-row__text">
                <b>
                  {scheme.name === 'multiyear'
                    ? 'Standard multi-year weights'
                    : humanize(scheme.name)}{' '}
                  ({scheme.bls_variable})
                </b>
                <span className="field-hint" style={{ display: 'block' }}>
                  {scheme.description} Valid years: {scheme.valid_years}.
                </span>
              </span>
            </label>
          ))}
          {state.weights === 'pandemic' ? (
            <p className="info-note" style={{ marginTop: '0.4rem' }}>
              The pandemic weighting is a distinct methodology for 2019–2020 only: estimates
              represent the comparable collection windows of 2020, not the full year. The result
              will carry the exact caveat.
            </p>
          ) : null}
        </fieldset>

        <fieldset style={{ marginTop: '0.9rem' }}>
          <legend>Uncertainty</legend>
          {varianceMethods.includes('replicate') ? (
            <label className="radio-row">
              <input
                type="radio"
                name={`${id}-variance`}
                checked={state.variance === 'replicate'}
                onChange={() => onChange({ variance: 'replicate' })}
              />
              <span className="radio-row__text">
                <b>With standard errors (recommended)</b>
                <span className="field-hint" style={{ display: 'block' }}>
                  Official replicate-weight standard errors and confidence intervals. Slower for
                  long periods.
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
                <b>Point estimate only</b>
                <span className="field-hint" style={{ display: 'block' }}>
                  Faster, but no uncertainty is reported.
                </span>
              </span>
            </label>
          ) : null}
        </fieldset>

        <div className="field" style={{ marginTop: '0.9rem', maxWidth: '12rem' }}>
          <label htmlFor={`${id}-confidence`}>Confidence level</label>
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
