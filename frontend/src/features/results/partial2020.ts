/**
 * 2020 in a trend, as a partial-year estimate.
 *
 * Under the standard multi-year weights the API has no 2020 estimate:
 * collection paused from March 18 to May 9, 2020, so there is no full-year
 * figure (ADR-004). Rather than leave a gap, a trend asks for 2020 alone
 * under the pandemic weights (TU20FWGT), which represent the weeks that
 * were collected, and draws that point hollow with a note. Every number is
 * still the API's own; this module only decides which request to send and
 * where its answer goes.
 */
import type {
  ActivitySelection,
  AnalysisRequest,
  EstimateRequest,
  EstimateResponse,
  TrendPoint,
} from '../../api/types'
import { fromRequest, toRequest } from '../builder/state'

export const PARTIAL_YEAR = 2020

interface PresetDefinition {
  name: string
  label: string
  include: string[]
  exclude: string[]
}

/** Whether a trend's 2020 needs the partial-year estimate (its weights have none). */
export function needsPartial2020(spec: AnalysisRequest): boolean {
  return spec.years.includes(PARTIAL_YEAR) && (spec.weights ?? 'multiyear') === 'multiyear'
}

const sorted = (codes: readonly string[]) => [...codes].sort()
const sameCodes = (a: readonly string[], b: readonly string[]) =>
  sorted(a).join() === sorted(b).join()

/**
 * The activity as a preset when it is exactly one — a link names the preset,
 * its canonical form names its codes under the preset's label — else its
 * codes in a fixed order, so both forms of one link send the same request.
 */
function stableActivity(
  activity: ActivitySelection,
  presets: readonly PresetDefinition[],
): ActivitySelection {
  if (activity.preset != null) return { preset: activity.preset }
  const include = activity.include ?? []
  const exclude = activity.exclude ?? []
  const preset = presets.find(
    (candidate) =>
      candidate.label === activity.label &&
      sameCodes(candidate.include, include) &&
      sameCodes(candidate.exclude, exclude),
  )
  if (preset) return { preset: preset.name }
  return {
    include: sorted(include),
    ...(exclude.length > 0 ? { exclude: sorted(exclude) } : {}),
    ...(activity.label != null ? { label: activity.label } : {}),
  }
}

/**
 * The request for a trend's 2020: the same analysis for 2020 alone under the
 * pandemic weights, written exactly as the builder writes that estimate, so
 * the static snapshot's prewarmed "pandemic weights" entries answer a
 * preset's (src/domain/prewarm.ts).
 */
export function partial2020Spec(
  spec: AnalysisRequest,
  availableYears: readonly number[],
  presets: readonly PresetDefinition[],
): EstimateRequest {
  const state = fromRequest(
    'estimate',
    {
      ...spec,
      activity: stableActivity(spec.activity, presets),
      years: [PARTIAL_YEAR],
      weights: 'pandemic',
    },
    availableYears,
  )
  return toRequest(state, availableYears) as EstimateRequest
}

/**
 * A trend's points with 2020 filled in from its partial-year estimate, and
 * the years so filled (drawn hollow). Without an answer, or when 2020
 * already has an estimate, the points are unchanged.
 */
export function withPartial2020(
  points: readonly TrendPoint[],
  partial: EstimateResponse | undefined,
): { points: TrendPoint[]; partialYears: number[] } {
  const index = points.findIndex((point) => point.year === PARTIAL_YEAR)
  const gap = points[index]
  if (partial === undefined || gap === undefined || gap.estimate != null) {
    return { points: [...points], partialYears: [] }
  }
  const filled: TrendPoint = {
    year: PARTIAL_YEAR,
    estimate: partial.estimate,
    n_respondents: partial.n_respondents,
    n_participants: partial.n_participants,
    weighted_population_per_day: partial.weighted_population_per_day,
    unavailable_reason: null,
  }
  return {
    points: points.map((point, position) => (position === index ? filled : point)),
    partialYears: [PARTIAL_YEAR],
  }
}
