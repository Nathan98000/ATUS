/**
 * A split trend draws no likely range: its lines are point estimates, and a
 * band per group would cost a replicate-weight trend each (review round 4).
 * Instead it links to a comparison of two of its groups in one year — the
 * latest year every group has an estimate — whose difference the API gives
 * with its likely range. The request is written exactly as the builder
 * writes that comparison (`toRequest`), so men vs women for a preset is
 * the snapshot's prewarmed "compare by sex" entry. Nothing is computed here:
 * the groups are chosen by the trend's own values (the two for two groups,
 * else the highest and the lowest).
 */
import type { AnalysisRequest, TrendResponse } from '../../api/types'
import { peoplePhrase } from '../../domain/describe'
import { analysisPath } from '../../domain/urlSpec'
import { defaultBuilderState, toRequest, weightsForSingleYear } from '../builder/state'
import {
  choiceDimensions,
  type CompareOption,
  type ResolvedSplit,
} from '../population/compareGroups'

export function splitComparison(
  spec: AnalysisRequest,
  split: ResolvedSplit,
  series: readonly TrendResponse[],
  availableYears: readonly number[],
): { path: string; text: string } | null {
  const years = [...spec.years].sort((a, b) => b - a)
  for (const year of years) {
    const values = series.map(
      (result) => result.points.find((point) => point.year === year)?.estimate?.value,
    )
    if (values.some((value) => value == null)) continue
    const ranked = split.options
      .map((option, index) => ({ option, value: values[index] as number }))
      .sort((a, b) => b.value - a.value)
    const pair: [CompareOption, CompareOption] | null =
      split.options.length === 2
        ? [split.options[0] as CompareOption, split.options[1] as CompareOption]
        : ranked.length >= 2
          ? [
              ranked[0]?.option as CompareOption,
              ranked[ranked.length - 1]?.option as CompareOption,
            ]
          : null
    if (pair === null) return null
    const [a, b] = pair
    const shared = { ...spec.population } as Record<string, unknown>
    for (const name of choiceDimensions(split.choice.by)) delete shared[name]
    const weights = spec.weights ?? 'multiyear'
    const request = toRequest(
      {
        ...defaultBuilderState(availableYears),
        operation: 'compare',
        activity: spec.activity,
        measure: spec.measure ?? 'average_minutes_per_day',
        population: shared,
        yearsMode: 'single',
        singleYear: year,
        groupA: a.population,
        groupB: b.population,
        labelA: a.label,
        labelB: b.label,
        compareBy: split.choice.by,
        weights,
        ...weightsForSingleYear(year, weights),
        // The point of the comparison is its likely range.
        variance: 'replicate',
        confidenceLevel: spec.confidence_level ?? 0.95,
      },
      availableYears,
    )
    const name = (option: CompareOption) =>
      option.population.day_type != null
        ? option.label.toLowerCase()
        : peoplePhrase({ ...shared, ...option.population })
    return {
      path: analysisPath('compare', request),
      text: `How sure is this? Compare ${name(a)} and ${name(b)} in ${year}, with the likely range of the difference`,
    }
  }
  return null
}
