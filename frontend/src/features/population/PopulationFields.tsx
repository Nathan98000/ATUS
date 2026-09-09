/**
 * Population filter controls, generated from GET /api/v1/population/metadata.
 * The API's dimension list is the source of valid dimensions and values —
 * nothing here enumerates filters on its own. Labels are presentation only.
 */
import { useId } from 'react'

import type { PopulationDimension, PopulationRequest } from '../../api/types'
import { dimensionLabel, stripVariableJargon, valueLabel } from '../../domain/describe'

/** Dimensions shown up front; everything else the API reports goes under "More filters". */
const PRIMARY_DIMENSIONS = new Set([
  'sex',
  'employment_status',
  'education_level',
  'has_household_children',
  'day_type',
])

interface PopulationFieldsProps {
  dimensions: readonly PopulationDimension[]
  value: PopulationRequest
  onChange: (value: PopulationRequest) => void
  ageError?: string
}

/** '25' → 25; '' or non-numeric input → undefined (never NaN in a spec). */
function numberOrUndefined(raw: string): number | undefined {
  if (raw === '') return undefined
  const parsed = Number(raw)
  return Number.isNaN(parsed) ? undefined : parsed
}

export function PopulationFields({
  dimensions,
  value,
  onChange,
  ageError,
}: PopulationFieldsProps) {
  const id = useId()
  const byName = new Map(dimensions.map((dimension) => [dimension.name, dimension]))
  const hasAge = byName.has('age_min') || byName.has('age_max')

  const set = (name: string, next: unknown) => {
    onChange({ ...value, [name]: next } as PopulationRequest)
  }

  const primary = dimensions.filter((dimension) => PRIMARY_DIMENSIONS.has(dimension.name))
  const secondary = dimensions.filter(
    (dimension) =>
      !PRIMARY_DIMENSIONS.has(dimension.name) &&
      !['age_min', 'age_max'].includes(dimension.name),
  )
  // Filters must never be active yet invisible: if a secondary dimension has
  // a value (e.g. from a shared analysis), its section starts open.
  const secondaryActive = secondary.some(
    (dimension) => (value as Record<string, unknown>)[dimension.name] != null,
  )

  return (
    <div>
      <div className="population-grid">
        {hasAge ? (
          <fieldset className="field">
            <legend>Age</legend>
            <div className="age-pair">
              <label className="visually-hidden" htmlFor={`${id}-age-min`}>
                Minimum age
              </label>
              <input
                id={`${id}-age-min`}
                type="number"
                inputMode="numeric"
                min={15}
                max={130}
                placeholder="Min"
                aria-invalid={ageError ? true : undefined}
                aria-describedby={`${id}-age-note`}
                value={value.age_min ?? ''}
                onChange={(event) => set('age_min', numberOrUndefined(event.target.value))}
              />
              <span aria-hidden="true">–</span>
              <label className="visually-hidden" htmlFor={`${id}-age-max`}>
                Maximum age
              </label>
              <input
                id={`${id}-age-max`}
                type="number"
                inputMode="numeric"
                min={15}
                max={130}
                placeholder="Max"
                aria-invalid={ageError ? true : undefined}
                aria-describedby={`${id}-age-note`}
                value={value.age_max ?? ''}
                onChange={(event) => set('age_max', numberOrUndefined(event.target.value))}
              />
            </div>
            {ageError ? (
              <p className="error-text" id={`${id}-age-note`}>
                {ageError}
              </p>
            ) : (
              <p className="field-hint" id={`${id}-age-note`}>
                Leave empty for all ages (15+).
              </p>
            )}
          </fieldset>
        ) : null}
        {primary.map((dimension) => (
          <DimensionField
            key={dimension.name}
            dimension={dimension}
            idPrefix={id}
            value={value}
            set={set}
          />
        ))}
      </div>
      {secondary.length > 0 ? (
        <details className="more-filters" open={secondaryActive || undefined}>
          <summary>More filters{secondaryActive ? ' (active)' : ''}</summary>
          <div className="population-grid" style={{ marginTop: '0.6rem' }}>
            {secondary.map((dimension) => (
              <DimensionField
                key={dimension.name}
                dimension={dimension}
                idPrefix={id}
                value={value}
                set={set}
              />
            ))}
          </div>
        </details>
      ) : null}
    </div>
  )
}

function DimensionField({
  dimension,
  idPrefix,
  value,
  set,
}: {
  dimension: PopulationDimension
  idPrefix: string
  value: PopulationRequest
  set: (name: string, next: unknown) => void
}) {
  const fieldId = `${idPrefix}-${dimension.name}`
  const current = (value as Record<string, unknown>)[dimension.name]

  if (dimension.type === 'boolean') {
    return (
      <div className="field">
        <label htmlFor={fieldId}>{dimensionLabel(dimension.name)}</label>
        <select
          id={fieldId}
          value={current === true ? 'true' : current === false ? 'false' : ''}
          onChange={(event) =>
            set(
              dimension.name,
              event.target.value === '' ? undefined : event.target.value === 'true',
            )
          }
        >
          <option value="">Any</option>
          <option value="true">Yes</option>
          <option value="false">No</option>
        </select>
        {dimension.description ? (
          <p className="field-hint">{stripVariableJargon(dimension.description)}</p>
        ) : null}
      </div>
    )
  }

  if (dimension.type === 'category' && dimension.values && dimension.values.length > 0) {
    return (
      <div className="field">
        <label htmlFor={fieldId}>{dimensionLabel(dimension.name)}</label>
        <select
          id={fieldId}
          value={current == null ? '' : String(current)}
          onChange={(event) => {
            const raw = event.target.value
            if (raw === '') return set(dimension.name, undefined)
            const original = (dimension.values as (string | number)[]).find(
              (candidate) => String(candidate) === raw,
            )
            set(dimension.name, original ?? raw)
          }}
        >
          <option value="">Any</option>
          {(dimension.values as (string | number)[]).map((option) => (
            <option key={String(option)} value={String(option)}>
              {valueLabel(dimension.name, option)}
            </option>
          ))}
        </select>
      </div>
    )
  }

  if (dimension.type === 'date') {
    return (
      <div className="field">
        <label htmlFor={fieldId}>{dimensionLabel(dimension.name)}</label>
        <input
          id={fieldId}
          type="date"
          value={typeof current === 'string' ? current : ''}
          onChange={(event) =>
            set(dimension.name, event.target.value === '' ? undefined : event.target.value)
          }
        />
        {dimension.description ? (
          <p className="field-hint">{stripVariableJargon(dimension.description)}</p>
        ) : null}
      </div>
    )
  }

  // Category without an enumerated value list (e.g. state FIPS): free text.
  return (
    <div className="field">
      <label htmlFor={fieldId}>{dimensionLabel(dimension.name)}</label>
      <input
        id={fieldId}
        type="text"
        value={typeof current === 'string' ? current : ''}
        placeholder="Any"
        onChange={(event) =>
          set(dimension.name, event.target.value === '' ? undefined : event.target.value)
        }
      />
      {dimension.description ? (
        <p className="field-hint">{stripVariableJargon(dimension.description)}</p>
      ) : null}
    </div>
  )
}
