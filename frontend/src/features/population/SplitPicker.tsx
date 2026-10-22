/**
 * A trend's "split into groups": one dimension, then which of its groups
 * to draw — one line each, like the comparison's "compare by" choices.
 */
import { useId } from 'react'

import type { PopulationDimension } from '../../api/types'
import type { BuilderState } from '../builder/state'
import { choiceDimensions, compareChoices, type CompareBy } from './compareGroups'

interface SplitPickerProps {
  state: BuilderState
  dimensions: readonly PopulationDimension[]
  error?: string
  onChange: (update: Partial<BuilderState>) => void
}

export function SplitPicker({ state, dimensions, error, onChange }: SplitPickerProps) {
  const id = useId()
  const choices = compareChoices(dimensions)
  const choice = choices.find((candidate) => candidate.by === state.splitBy)

  const chooseSplit = (value: string) => {
    const next = choices.find((candidate) => candidate.by === value)
    if (!next) {
      onChange({ splitBy: null, splitGroups: [] })
      return
    }
    // A filter on the split dimension would leave one line: drop it.
    const population = { ...state.population } as Record<string, unknown>
    for (const name of choiceDimensions(next.by as CompareBy)) delete population[name]
    onChange({
      splitBy: next.by,
      splitGroups: next.options.map((option) => option.key),
      population,
    })
  }

  const toggle = (key: string, checked: boolean) => {
    if (!choice) return
    const chosen = new Set(state.splitGroups)
    if (checked) chosen.add(key)
    else chosen.delete(key)
    // Kept in the choice's own order, so lines and colours never reshuffle.
    onChange({
      splitGroups: choice.options
        .map((option) => option.key)
        .filter((candidate) => chosen.has(candidate)),
    })
  }

  return (
    <div className="split-picker">
      <div className="field split-picker__by">
        <label htmlFor={`${id}-split`}>Lines</label>
        <select
          id={`${id}-split`}
          value={choice?.by ?? 'none'}
          onChange={(event) => chooseSplit(event.target.value)}
        >
          <option value="none">One line for everyone</option>
          {choices.map((candidate) => (
            <option key={candidate.by} value={candidate.by}>
              One line per group: {candidate.label.toLowerCase()}
            </option>
          ))}
        </select>
      </div>
      {choice ? (
        <fieldset className="split-picker__groups">
          <legend>Show groups</legend>
          <div className="split-picker__options">
            {choice.options.map((option) => (
              <label key={option.key} className="split-picker__option">
                <input
                  type="checkbox"
                  checked={state.splitGroups.includes(option.key)}
                  onChange={(event) => toggle(option.key, event.target.checked)}
                />
                <span>{option.label}</span>
              </label>
            ))}
          </div>
          {error ? <p className="error-text">{error}</p> : null}
        </fieldset>
      ) : null}
    </div>
  )
}
