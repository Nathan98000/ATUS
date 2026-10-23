/**
 * The chart being built, in one line, with the "Show chart" button. It stays
 * in view while the builder scrolls: beside the form on wide screens, as a
 * bar along the bottom of the screen on narrow ones — so showing the chart
 * never means scrolling to the end of the form first. On the narrow bar the
 * activity takes one line and who and when always the next; the statistic,
 * the "Show" select right under the activity, is left out there (review
 * round 6: clamped to two lines, the bar hid who and when).
 */
import { useMemo } from 'react'

import { useActivityPresets } from '../../api/queries'
import {
  activityLabelFromSpec,
  activityName,
  measureName,
  populationPhrase,
  sharedPopulationPhrase,
} from '../../domain/describe'
import { formatYears } from '../../utils/format'
import type { PopulationDimension } from '../../api/types'
import { compareChoices } from '../population/compareGroups'
import { selectedYears, toRequest, type BuilderState } from './state'

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
    ? activityName(activityLabelFromSpec(request, presetLabels))
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
      const shared = sharedPopulationPhrase(request)
      who = `${groups.join(', ')}${shared === null ? '' : ` (all: ${shared})`}`
    }
  }
  const years = selectedYears(state, availableYears)
  const when =
    years.length === 0
      ? 'No years chosen'
      : state.operation !== 'trend' && years.length > 1
        ? `${formatYears(years)}, combined`
        : formatYears(years)

  return (
    <section className="builder-summary" aria-labelledby="builder-summary-heading">
      <h2 id="builder-summary-heading" className="builder-summary__heading">
        Your chart
      </h2>
      <p className="builder-summary__text">
        <b className="builder-summary__activity">{activity}</b>
        <span className="builder-summary__measure">
          <span className="builder-summary__sep"> · </span>
          {measureName(state.measure)}
        </span>
        <span className="builder-summary__who">
          <span className="builder-summary__sep"> · </span>
          {who}
          {/* When stays whole, and a line never starts with its dot. */}
          <span className="builder-summary__when">{`\u00a0· ${when}`}</span>
        </span>
      </p>
      <div className="builder-summary__actions">
        <button
          type="button"
          className="btn btn--primary builder-summary__analyze"
          onClick={onAnalyze}
        >
          Show chart
        </button>
        <p className="field-hint builder-summary__hint">
          Each chart gets its own link to share.
        </p>
      </div>
    </section>
  )
}
