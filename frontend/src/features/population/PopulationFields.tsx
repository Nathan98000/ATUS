/**
 * Population filters, opt-in: "Everyone age 15 and over" until the visitor
 * adds one from a short list — sex, age, employment, education, children in
 * the household, day of week — each a row with its own remove button.
 *
 * The API's dimension list is the source of valid dimensions and values —
 * nothing here enumerates filters on its own, and only the dimensions above
 * that the API reports are offered. Any other dimension (region, state,
 * diary dates, or one added to the API later) is never offered, but a
 * shared link that sets one still shows it as a row, so a filter is never
 * active yet invisible. Labels are presentation only.
 */
import { useId, useState } from 'react'

import type { PopulationDimension, PopulationRequest } from '../../api/types'
import { dimensionLabel, stripVariableJargon, valueLabel } from '../../domain/describe'

/** The filters offered, in this order ("age" stands for age_min/age_max). */
const OFFERED = [
  'sex',
  'age',
  'employment_status',
  'education_level',
  'has_household_children',
  'day_type',
]

const AGE_FIELDS = ['age_min', 'age_max']

interface PopulationFieldsProps {
  dimensions: readonly PopulationDimension[]
  value: PopulationRequest
  onChange: (value: PopulationRequest) => void
  ageError?: string
  /** What no filter means here (default "Everyone age 15 and over."). */
  emptyText?: string
}

/** '25' → 25; '' or non-numeric input → undefined (never NaN in a spec). */
function numberOrUndefined(raw: string): number | undefined {
  if (raw === '') return undefined
  const parsed = Number(raw)
  return Number.isNaN(parsed) ? undefined : parsed
}

function filterLabel(name: string): string {
  return name === 'age' ? 'Age' : dimensionLabel(name)
}

export function PopulationFields({
  dimensions,
  value,
  onChange,
  ageError,
  emptyText = 'Everyone age 15 and over.',
}: PopulationFieldsProps) {
  const id = useId()
  // Filters added in this session that may not have a value yet.
  const [added, setAdded] = useState<string[]>([])
  const byName = new Map(dimensions.map((dimension) => [dimension.name, dimension]))
  const current = value as Record<string, unknown>
  const isSet = (name: string) => current[name] != null

  const offered = OFFERED.filter((name) =>
    name === 'age' ? AGE_FIELDS.some((field) => byName.has(field)) : byName.has(name),
  )
  const active = (name: string) => (name === 'age' ? AGE_FIELDS.some(isSet) : isSet(name))
  const shown = offered.filter((name) => active(name) || added.includes(name))
  // Set by a link but never offered: shown, so it is never active yet invisible.
  const others = dimensions
    .map((dimension) => dimension.name)
    .filter((name) => !AGE_FIELDS.includes(name) && !offered.includes(name))
    .filter(isSet)
  const available = offered.filter((name) => !shown.includes(name))

  const set = (name: string, next: unknown) => {
    // A filter being edited stays on screen even when cleared (it came from
    // a link or another analysis, so it was not "added" here): its row
    // never vanishes under the cursor. Only its remove button removes it.
    const filter = AGE_FIELDS.includes(name) ? 'age' : name
    setAdded((list) => (list.includes(filter) ? list : [...list, filter]))
    onChange({ ...value, [name]: next } as PopulationRequest)
  }

  const add = (name: string) => {
    if (name === '') return
    setAdded((list) => [...list, name])
    // Move to the new filter's control once it renders.
    requestAnimationFrame(() => {
      document.getElementById(`${id}-${name === 'age' ? 'age_min' : name}`)?.focus()
    })
  }

  const remove = (name: string) => {
    setAdded((list) => list.filter((candidate) => candidate !== name))
    const next = { ...current }
    for (const field of name === 'age' ? AGE_FIELDS : [name]) delete next[field]
    onChange(next as PopulationRequest)
  }

  return (
    <div className="population-filters">
      {shown.length === 0 && others.length === 0 ? (
        <p className="population-filters__empty">{emptyText}</p>
      ) : (
        <ul className="population-filters__list">
          {[...shown, ...others].map((name) => (
            <li key={name} className="population-filter">
              {name === 'age' ? (
                <AgeField idPrefix={id} value={value} set={set} ageError={ageError} />
              ) : (
                <DimensionField
                  dimension={byName.get(name) as PopulationDimension}
                  idPrefix={id}
                  value={value}
                  set={set}
                  showHint={!offered.includes(name)}
                />
              )}
              <button
                type="button"
                className="population-filter__remove"
                aria-label={`Remove the ${filterLabel(name).toLowerCase()} filter`}
                onClick={() => remove(name)}
              >
                <span aria-hidden="true">×</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {available.length > 0 ? (
        <div className="population-filters__add">
          <label className="visually-hidden" htmlFor={`${id}-add`}>
            Add a filter
          </label>
          <select
            id={`${id}-add`}
            className="population-filters__add-select"
            value=""
            onChange={(event) => add(event.target.value)}
          >
            <option value="">+ Add a filter</option>
            {available.map((name) => (
              <option key={name} value={name}>
                {filterLabel(name)}
              </option>
            ))}
          </select>
        </div>
      ) : null}
    </div>
  )
}

function AgeField({
  idPrefix,
  value,
  set,
  ageError,
}: {
  idPrefix: string
  value: PopulationRequest
  set: (name: string, next: unknown) => void
  ageError?: string
}) {
  return (
    <fieldset className="field population-filter__field">
      <legend>Age</legend>
      <div className="age-pair">
        <label className="visually-hidden" htmlFor={`${idPrefix}-age_min`}>
          Minimum age
        </label>
        <input
          id={`${idPrefix}-age_min`}
          type="number"
          inputMode="numeric"
          min={15}
          max={130}
          placeholder="15"
          aria-invalid={ageError ? true : undefined}
          aria-describedby={ageError ? `${idPrefix}-age-note` : undefined}
          value={value.age_min ?? ''}
          onChange={(event) => set('age_min', numberOrUndefined(event.target.value))}
        />
        <span aria-hidden="true">to</span>
        <label className="visually-hidden" htmlFor={`${idPrefix}-age_max`}>
          Maximum age
        </label>
        <input
          id={`${idPrefix}-age_max`}
          type="number"
          inputMode="numeric"
          min={15}
          max={130}
          placeholder="85+"
          aria-invalid={ageError ? true : undefined}
          aria-describedby={ageError ? `${idPrefix}-age-note` : undefined}
          value={value.age_max ?? ''}
          onChange={(event) => set('age_max', numberOrUndefined(event.target.value))}
        />
      </div>
      {ageError ? (
        <p className="error-text" id={`${idPrefix}-age-note`}>
          {ageError}
        </p>
      ) : null}
    </fieldset>
  )
}

function DimensionField({
  dimension,
  idPrefix,
  value,
  set,
  showHint,
}: {
  dimension: PopulationDimension
  idPrefix: string
  value: PopulationRequest
  set: (name: string, next: unknown) => void
  /** The API's description, for a dimension the list does not offer. */
  showHint: boolean
}) {
  const fieldId = `${idPrefix}-${dimension.name}`
  const current = (value as Record<string, unknown>)[dimension.name]
  const label = filterLabel(dimension.name)
  const hint =
    showHint && dimension.description ? (
      <p className="field-hint">{stripVariableJargon(dimension.description)}</p>
    ) : null

  if (dimension.type === 'boolean') {
    return (
      <div className="field population-filter__field">
        <label htmlFor={fieldId}>{label}</label>
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
        {hint}
      </div>
    )
  }

  if (dimension.type === 'category' && dimension.values && dimension.values.length > 0) {
    return (
      <div className="field population-filter__field">
        <label htmlFor={fieldId}>{label}</label>
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
        {hint}
      </div>
    )
  }

  if (dimension.type === 'date') {
    return (
      <div className="field population-filter__field">
        <label htmlFor={fieldId}>{label}</label>
        <input
          id={fieldId}
          type="date"
          value={typeof current === 'string' ? current : ''}
          onChange={(event) =>
            set(dimension.name, event.target.value === '' ? undefined : event.target.value)
          }
        />
        {hint}
      </div>
    )
  }

  // Category without an enumerated value list (e.g. state FIPS): free text.
  return (
    <div className="field population-filter__field">
      <label htmlFor={fieldId}>{label}</label>
      <input
        id={fieldId}
        type="text"
        value={typeof current === 'string' ? current : ''}
        placeholder="Any"
        onChange={(event) =>
          set(dimension.name, event.target.value === '' ? undefined : event.target.value)
        }
      />
      {hint}
    </div>
  )
}
