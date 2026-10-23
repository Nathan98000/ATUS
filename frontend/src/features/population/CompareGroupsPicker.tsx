/**
 * The two groups of a comparison: "Compare by" one dimension and pick its
 * two values, or build custom groups with the full filters. Filters that
 * apply to both groups sit below, added one at a time.
 */
import { useId } from 'react'

import type { PopulationDimension } from '../../api/types'
import type { BuilderState } from '../builder/state'
import {
  choiceDimensions,
  compareChoices,
  defaultPair,
  type CompareBy,
  type CompareDimensionChoice,
} from './compareGroups'
import { PopulationFields } from './PopulationFields'

interface CompareGroupsPickerProps {
  state: BuilderState
  dimensions: readonly PopulationDimension[]
  errors: { age?: string; ageA?: string; ageB?: string }
  onChange: (update: Partial<BuilderState>) => void
}

export function CompareGroupsPicker({
  state,
  dimensions,
  errors,
  onChange,
}: CompareGroupsPickerProps) {
  const id = useId()
  const choices = compareChoices(dimensions)
  const choice = choices.find((candidate) => candidate.by === state.compareBy)
  const compareBy: CompareBy = choice ? choice.by : 'custom'

  const chooseDimension = (next: CompareBy) => {
    if (next === 'custom') {
      // Keep the current groups, now editable in full.
      onChange({ compareBy: 'custom' })
      return
    }
    const nextChoice = choices.find((candidate) => candidate.by === next)
    if (!nextChoice) return
    const [a, b] = defaultPair(nextChoice)
    // Shared filters on the compared dimension would contradict the groups.
    const population = { ...state.population }
    for (const name of choiceDimensions(next))
      delete (population as Record<string, unknown>)[name]
    onChange({
      compareBy: next,
      groupA: a.population,
      groupB: b.population,
      labelA: a.label,
      labelB: b.label,
      population,
    })
  }

  const chooseValue = (activeChoice: CompareDimensionChoice, group: 'A' | 'B', key: string) => {
    const picked = activeChoice.options.find((candidate) => candidate.key === key)
    if (!picked) return
    onChange(
      group === 'A'
        ? { groupA: picked.population, labelA: picked.label }
        : { groupB: picked.population, labelB: picked.label },
    )
  }

  const keyOf = (activeChoice: CompareDimensionChoice, label: string) =>
    activeChoice.options.find((candidate) => candidate.label === label)?.key ?? ''

  return (
    <div className="compare-groups">
      <div className="compare-groups__row">
        <div className="field compare-groups__by">
          <label htmlFor={`${id}-by`}>Compare by</label>
          <select
            id={`${id}-by`}
            value={compareBy}
            onChange={(event) => chooseDimension(event.target.value as CompareBy)}
          >
            {choices.map((candidate) => (
              <option key={candidate.by} value={candidate.by}>
                {candidate.label}
              </option>
            ))}
            <option value="custom">Custom groups…</option>
          </select>
        </div>
        {choice ? (
          <div className="compare-groups__pair">
            {(['A', 'B'] as const).map((group, index) => (
              <div key={group} className="field compare-groups__value">
                <label htmlFor={`${id}-${group}`}>
                  <span
                    className={`compare-groups__swatch compare-groups__swatch--${group.toLowerCase()}`}
                    aria-hidden="true"
                  />
                  {index === 0 ? 'First group' : 'Second group'}
                </label>
                <select
                  id={`${id}-${group}`}
                  value={keyOf(choice, group === 'A' ? state.labelA : state.labelB)}
                  onChange={(event) => chooseValue(choice, group, event.target.value)}
                >
                  {choice.options.map((candidate) => (
                    <option key={candidate.key} value={candidate.key}>
                      {candidate.label}
                    </option>
                  ))}
                </select>
              </div>
            ))}
          </div>
        ) : null}
      </div>

      {choice && state.labelA === state.labelB ? (
        <p className="error-text">Choose two different groups.</p>
      ) : null}

      {compareBy === 'custom' ? (
        <div className="group-editors">
          {(
            [
              {
                key: 'A',
                label: state.labelA,
                population: state.groupA,
                ageError: errors.ageA,
              },
              {
                key: 'B',
                label: state.labelB,
                population: state.groupB,
                ageError: errors.ageB,
              },
            ] as const
          ).map((group) => (
            <fieldset
              key={group.key}
              className={`group-editor group-editor--${group.key.toLowerCase()}`}
            >
              <legend>Group {group.key}</legend>
              <div className="field" style={{ marginBottom: '0.6rem' }}>
                <label htmlFor={`group-label-${group.key}`}>Name</label>
                <input
                  id={`group-label-${group.key}`}
                  type="text"
                  maxLength={80}
                  value={group.label}
                  onChange={(event) =>
                    onChange(
                      group.key === 'A'
                        ? { labelA: event.target.value }
                        : { labelB: event.target.value },
                    )
                  }
                />
              </div>
              <PopulationFields
                dimensions={dimensions}
                value={group.population}
                onChange={(population) =>
                  onChange(group.key === 'A' ? { groupA: population } : { groupB: population })
                }
                ageError={group.ageError}
              />
            </fieldset>
          ))}
        </div>
      ) : null}

      {/* Filters for both groups: opt-in rows, each always visible once set. */}
      <fieldset className="compare-groups__shared">
        <legend className="compare-groups__shared-label">Both groups</legend>
        <PopulationFields
          dimensions={dimensions.filter(
            (dimension) => !choiceDimensions(compareBy).includes(dimension.name),
          )}
          value={state.population}
          onChange={(population) => onChange({ population })}
          ageError={errors.age}
          emptyText="Everyone age 15 and over."
        />
      </fieldset>
    </div>
  )
}
