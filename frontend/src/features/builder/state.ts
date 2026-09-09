/**
 * Analysis-builder state: the UI's working copy of an analysis before it is
 * committed to a URL and run. Converts losslessly to and from the API's
 * request specs (`toRequest` / `fromRequest`), so shared links can be opened
 * back into the builder for editing.
 */
import type {
  ActivitySelection,
  AnalysisRequest,
  CompareRequest,
  EstimateRequest,
  Operation,
  PopulationRequest,
} from '../../api/types'

export type Measure = NonNullable<EstimateRequest['measure']>
export type WeightScheme = NonNullable<EstimateRequest['weights']>
export type VarianceMethod = NonNullable<EstimateRequest['variance']>

/**
 * Statistics this UI knows how to request and display. Intersected with the
 * capabilities reported by GET /meta — a measure the server stops reporting
 * disappears from the UI, and a future server-side measure is ignored until
 * the UI (and its generated types) learn it.
 */
export const KNOWN_MEASURES = [
  'average_minutes_per_day',
  'participation_rate',
  'average_minutes_per_participant',
  'participants_per_day',
] as const satisfies readonly Measure[]

export const KNOWN_WEIGHT_SCHEMES = [
  'multiyear',
  'pandemic',
] as const satisfies readonly WeightScheme[]

export type YearsMode = 'single' | 'pooled'

export interface BuilderState {
  operation: Operation
  activity: ActivitySelection
  /** One-time note when a prefilled shared analysis could not be represented exactly. */
  prefillNote?: string
  measure: Measure
  /** estimate/compare: one year or a pooled set. */
  yearsMode: YearsMode
  singleYear: number | null
  pooledYears: number[]
  /** trend: inclusive year range. */
  trendFrom: number | null
  trendTo: number | null
  population: PopulationRequest
  groupA: PopulationRequest
  groupB: PopulationRequest
  labelA: string
  labelB: string
  weights: WeightScheme
  variance: VarianceMethod
  confidenceLevel: number
}

export function defaultBuilderState(availableYears: readonly number[]): BuilderState {
  const latestYear = availableYears.length > 0 ? Math.max(...availableYears) : null
  const earliestYear = availableYears.length > 0 ? Math.min(...availableYears) : null
  return {
    operation: 'estimate',
    activity: { preset: 'sleep' },
    measure: 'average_minutes_per_day',
    yearsMode: 'single',
    singleYear: latestYear,
    pooledYears: [],
    trendFrom: earliestYear,
    trendTo: latestYear,
    population: {},
    groupA: {},
    groupB: {},
    labelA: 'Group A',
    labelB: 'Group B',
    weights: 'multiyear',
    variance: 'replicate',
    confidenceLevel: 0.95,
  }
}

/** Drops unset (null/undefined) population fields so specs stay minimal. */
export function prunePopulation(population: PopulationRequest): PopulationRequest {
  const pruned: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(population)) {
    if (value !== null && value !== undefined) pruned[key] = value
  }
  return pruned as PopulationRequest
}

export function selectedYears(
  state: BuilderState,
  availableYears: readonly number[],
): number[] {
  if (state.operation === 'trend') {
    if (state.trendFrom === null || state.trendTo === null) return []
    return availableYears.filter(
      (year) => year >= (state.trendFrom as number) && year <= (state.trendTo as number),
    )
  }
  if (state.yearsMode === 'single') {
    return state.singleYear === null ? [] : [state.singleYear]
  }
  return [...state.pooledYears].sort((a, b) => a - b)
}

export interface BuilderValidation {
  errors: Partial<Record<'years' | 'age' | 'ageA' | 'ageB' | 'activity', string>>
  valid: boolean
}

/**
 * Fast local validation for immediate feedback only — the API remains the
 * authoritative validator for everything analytical.
 */
export function validateBuilder(
  state: BuilderState,
  availableYears: readonly number[],
): BuilderValidation {
  const errors: BuilderValidation['errors'] = {}

  if (selectedYears(state, availableYears).length === 0) {
    errors.years =
      state.operation === 'trend' ? 'Choose a year range.' : 'Choose at least one year.'
  }
  if (
    state.operation === 'trend' &&
    state.trendFrom !== null &&
    state.trendTo !== null &&
    state.trendFrom > state.trendTo
  ) {
    errors.years = 'The start year cannot be after the end year.'
  }

  const agesInvalid = (population: PopulationRequest) =>
    population.age_min != null &&
    population.age_max != null &&
    population.age_min > population.age_max
  if (agesInvalid(state.population)) errors.age = 'Minimum age cannot exceed maximum age.'
  if (state.operation === 'compare') {
    if (agesInvalid(state.groupA)) errors.ageA = 'Minimum age cannot exceed maximum age.'
    if (agesInvalid(state.groupB)) errors.ageB = 'Minimum age cannot exceed maximum age.'
  }

  if (
    !state.activity.preset &&
    !(state.activity.include && state.activity.include.length > 0)
  ) {
    errors.activity = 'Choose an activity.'
  }

  return { errors, valid: Object.keys(errors).length === 0 }
}

/** Builds the API request body for the current builder state. */
export function toRequest(
  state: BuilderState,
  availableYears: readonly number[],
): AnalysisRequest {
  const base: EstimateRequest = {
    measure: state.measure,
    activity: state.activity,
    years: selectedYears(state, availableYears),
    population: prunePopulation(state.population),
    weights: state.weights,
    variance: state.variance,
    confidence_level: state.confidenceLevel,
  }
  if (state.operation !== 'compare') return base
  return {
    ...base,
    group_a: prunePopulation(state.groupA),
    group_b: prunePopulation(state.groupB),
    // Empty names would make the result page and difference labels unreadable.
    label_a: state.labelA.trim() || 'Group A',
    label_b: state.labelB.trim() || 'Group B',
  } satisfies CompareRequest
}

/** Rebuilds builder state from a request spec (deep links, "adjust analysis"). */
export function fromRequest(
  operation: Operation,
  spec: AnalysisRequest,
  availableYears: readonly number[],
): BuilderState {
  const state = defaultBuilderState(availableYears)
  state.operation = operation
  state.activity = spec.activity
  if (spec.measure && (KNOWN_MEASURES as readonly string[]).includes(spec.measure)) {
    state.measure = spec.measure
  }
  if (spec.weights) state.weights = spec.weights
  if (spec.variance) state.variance = spec.variance
  if (spec.confidence_level != null) state.confidenceLevel = spec.confidence_level
  state.population = spec.population ?? {}

  // The builder can only represent years that exist in the loaded data,
  // each at most once; anything else is dropped WITH a visible note rather
  // than silently changing the shared analysis.
  const requested = [...spec.years].sort((a, b) => a - b)
  const years = [...new Set(requested)].filter(
    (year) => availableYears.length === 0 || availableYears.includes(year),
  )
  if (years.length !== requested.length) {
    state.prefillNote =
      'Some years in the shared analysis are not in the loaded data release and were dropped from the builder.'
  }
  if (operation === 'trend') {
    state.trendFrom = years[0] ?? state.trendFrom
    state.trendTo = years[years.length - 1] ?? state.trendTo
    const first = years[0]
    const contiguousInData =
      first !== undefined &&
      years.every((year, index) => index === 0 || availableYears.includes(year)) &&
      years.length ===
        availableYears.filter(
          (y) => y >= (years[0] as number) && y <= (years[years.length - 1] as number),
        ).length
    if (years.length > 0 && !contiguousInData) {
      state.prefillNote =
        'The shared trend skipped some years; the builder represents trends as full ranges, so the range was filled in.'
    }
  } else if (years.length === 1) {
    state.yearsMode = 'single'
    state.singleYear = years[0] ?? null
  } else {
    state.yearsMode = 'pooled'
    state.pooledYears = years
  }

  if (operation === 'compare') {
    const compare = spec as CompareRequest
    state.groupA = compare.group_a ?? {}
    state.groupB = compare.group_b ?? {}
    state.labelA = compare.label_a ?? 'Group A'
    state.labelB = compare.label_b ?? 'Group B'
  }
  return state
}
