/**
 * The analysis being built, in a few words, with the Analyze button. It
 * stays in view while the builder scrolls: beside the form on wide screens,
 * as a bar along the bottom of the screen on narrow ones — so running the
 * analysis never means scrolling to the end of a long form first.
 */
import { useMemo } from 'react'

import { useActivityPresets } from '../../api/queries'
import {
  activityLabelFromSpec,
  measureName,
  populationPhrase,
  sharedPopulationPhrase,
  splitActivityLabel,
} from '../../domain/describe'
import { formatYears } from '../../utils/format'
import type { PopulationDimension } from '../../api/types'
import { compareChoices } from '../population/compareGroups'
import { selectedYears, toRequest, type BuilderState } from './state'

const OPERATION_NAMES = {
  estimate: 'One number',
  trend: 'Change over time',
  compare: 'Comparison of two groups',
} as const

export function BuilderSummary({
  state,
  availableYears,
  dimensions,
  onAnalyze,
}: {
  state: BuilderState
  availableYears: readonly number[]
  /** The population metadata's dimensions, to name a split trend's groups. */
  dimensions: readonly PopulationDimension[]
  onAnalyze: () => void
}) {
  const presets = useActivityPresets()
  const presetLabels = useMemo(() => {
    const map = new Map<string, string>()
    for (const preset of presets.data?.presets ?? []) map.set(preset.name, preset.label)
    return map
  }, [presets.data])

  const request = toRequest(state, availableYears)
  const hasActivity = state.activity.preset != null || (state.activity.include?.length ?? 0) > 0
  const activity = hasActivity
    ? splitActivityLabel(activityLabelFromSpec(request, presetLabels)).name
    : 'No activity chosen'
  let who: string
  if (state.operation === 'compare') {
    const shared = sharedPopulationPhrase(request)
    const groups = `${state.labelA.trim() || 'Group A'} vs ${state.labelB.trim() || 'Group B'}`
    who = shared !== null ? `${groups} (both: ${shared})` : groups
  } else {
    who = populationPhrase(request.population)
    const split = compareChoices(dimensions).find((choice) => choice.by === state.splitBy)
    if (state.operation === 'trend' && split) {
      const groups = split.options
        .filter((option) => state.splitGroups.includes(option.key))
        .map((option) => option.label)
      who = `${groups.join(', ')}${who.startsWith('All respondents') ? '' : ` (all: ${who})`}`
    }
  }
  const years = selectedYears(state, availableYears)
  const when =
    years.length === 0
      ? 'No years chosen'
      : state.operation !== 'trend' && years.length > 1
        ? `${formatYears(years)}, combined`
        : formatYears(years)

  // The type comes last so the one-line form on narrow screens leads with
  // the activity; wide screens show it first (CSS order).
  const items = [
    { term: 'Doing what', detail: activity },
    { term: 'For whom', detail: who },
    { term: 'When', detail: when },
    { term: 'Measured as', detail: measureName(state.measure) },
    { term: 'Type', detail: OPERATION_NAMES[state.operation], type: true },
  ]

  return (
    <section className="builder-summary" aria-labelledby="builder-summary-heading">
      <h2 id="builder-summary-heading" className="builder-summary__heading">
        Your analysis
      </h2>
      <dl className="builder-summary__list">
        {items.map((item) => (
          <div
            key={item.term}
            className={`builder-summary__item${item.type ? ' builder-summary__item--type' : ''}`}
          >
            <dt>{item.term}</dt>
            <dd>{item.detail}</dd>
          </div>
        ))}
      </dl>
      <div className="builder-summary__actions">
        <button
          type="button"
          className="btn btn--primary builder-summary__analyze"
          onClick={onAnalyze}
        >
          Analyze
        </button>
        <p className="field-hint builder-summary__hint">
          Running an analysis creates a shareable link.
        </p>
      </div>
    </section>
  )
}
