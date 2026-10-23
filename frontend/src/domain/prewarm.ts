/**
 * The analyses each deploy prewarms into the static snapshot (ADR-011, tier
 * L0), starting with the home page's starter examples.
 *
 * Every entry is a request spec in exactly the shape the UI sends: starters
 * as the home page links them, everything else as the analysis builder
 * produces it from its defaults (`toRequest` itself builds them, so the two
 * cannot drift). `scripts/snapshot-requests.mjs` serializes these specs with
 * the SPA's own `stableStringify`, so the snapshot's request keys match the
 * bytes the SPA sends for the same analysis.
 *
 * This module is also loaded by Node (type stripping) from that script, so its
 * value imports carry explicit `.ts` extensions and import nothing that
 * touches the DOM.
 */
import type {
  AnalysisRequest,
  EstimateRequest,
  Operation,
  PopulationRequest,
  TrendRequest,
} from '../api/types'
import type { BuilderState } from '../features/builder/state.ts'
import {
  KNOWN_MEASURES,
  PARTIAL_YEAR,
  defaultBuilderState,
  fromRequest,
  toRequest,
} from '../features/builder/state.ts'
import { needsPartial2020, partial2020Spec } from '../features/results/partial2020.ts'
import { stableStringify } from '../utils/stableStringify.ts'

export interface StarterExample {
  question: string
  detail: string
  operation: Operation
  spec: (years: readonly number[], latest: number) => AnalysisRequest
}

/**
 * Real, runnable specifications linked from the home page, built against the
 * years the API reports. Their exact bytes are part of published share links,
 * so they must not be rewritten casually.
 */
export const STARTER_EXAMPLES: readonly StarterExample[] = [
  {
    question: 'How much do Americans sleep?',
    detail: 'Average time per day, most recent year',
    operation: 'estimate',
    spec: (_years, latest) => ({
      measure: 'average_minutes_per_day',
      activity: { preset: 'sleep' },
      years: [latest],
    }),
  },
  {
    question: 'How has leisure time changed?',
    detail: 'Yearly trend across the full period',
    operation: 'trend',
    spec: (years) => ({
      measure: 'average_minutes_per_day',
      activity: { preset: 'leisure_and_sports_bls_table' },
      years: [...years],
    }),
  },
  {
    question: 'Who watches more TV — men or women?',
    detail: 'Two groups and their difference, most recent year',
    operation: 'compare',
    spec: (_years, latest) => ({
      measure: 'average_minutes_per_day',
      activity: { preset: 'watching_tv' },
      years: [latest],
      group_a: { sex: 'male' },
      group_b: { sex: 'female' },
      label_a: 'Men',
      label_b: 'Women',
    }),
  },
  {
    question: 'Does household work differ with children at home?',
    detail: 'Households with vs without children under 18',
    operation: 'compare',
    spec: (_years, latest) => ({
      measure: 'average_minutes_per_day',
      activity: { preset: 'household_activities_bls_table' },
      years: [latest],
      group_a: { has_household_children: true },
      group_b: { has_household_children: false },
      label_a: 'With household children',
      label_b: 'Without household children',
    }),
  },
]

/**
 * The home page's chart (review round 4): household activities, men and
 * women, across every year — a trend split by sex, as the analysis page
 * draws `/analysis/trend?spec=…&split=sex&groups=male,female`. It is read
 * from the static snapshot only, so its requests are prewarmed here, in
 * the bytes that page sends: each group's point-estimate trend
 * (`seriesSpec`'s form) and each group's 2020 as its partial-year estimate
 * (`partial2020Spec`).
 */
export const HOME_CHART = {
  preset: 'household_activities_bls_table',
  split: { by: 'sex', groups: ['male', 'female'] },
} as const

export interface HomeChartSpecs {
  /** The split trend's own request, as its link encodes it. */
  trend: AnalysisRequest
  series: {
    key: string
    population: PopulationRequest
    spec: TrendRequest
    /** The group's 2020 under the pandemic weights, when the trend runs through 2020. */
    partial2020: EstimateRequest | null
  }[]
}

export function homeChartSpecs(years: readonly number[]): HomeChartSpecs {
  const trend = toRequest(
    {
      ...defaultBuilderState(years),
      operation: 'trend',
      activity: { preset: HOME_CHART.preset },
    },
    years,
  )
  return {
    trend,
    series: HOME_CHART.split.groups.map((sex) => {
      const population: PopulationRequest = { sex }
      const spec: TrendRequest = { ...trend, population, variance: 'none' }
      return {
        key: sex,
        population,
        spec,
        partial2020: needsPartial2020(spec) ? partial2020Spec(spec, years, []) : null,
      }
    }),
  }
}

/** What the prewarm list is built from: the deployed release's own metadata. */
export interface PrewarmInput {
  /** `/meta` → data.years */
  years: readonly number[]
  /** `/meta` → capabilities.activity_presets */
  presets: readonly string[]
  /** `/meta` → capabilities.measures[].name */
  measures: readonly string[]
  /** `/meta` → capabilities.weight_schemes[].name */
  weightSchemes: readonly string[]
  /** `/activities` → the tier-1 (two-digit) categories */
  tier1: readonly { code: string; name: string }[]
}

export interface PrewarmedAnalysis {
  name: string
  operation: Operation
  spec: AnalysisRequest
}

/**
 * About 135 analyses: the four starters; every preset under every measure
 * (latest year); a full-period trend per preset and per tier-1 category; a
 * latest-year estimate per tier-1 category; men vs women per preset and per
 * tier-1 category; each preset in 2020 (a trend's partial year); the home
 * page's chart (two group trends and their 2020s); and each starter as the
 * builder sends it after "Adjust analysis" without a change.
 * Duplicates (same operation and bytes) are dropped, keeping the first.
 */
export function prewarmedAnalyses(input: PrewarmInput): PrewarmedAnalysis[] {
  const { years } = input
  if (years.length === 0) return []
  const latest = Math.max(...years)
  const measures = KNOWN_MEASURES.filter((measure) => input.measures.includes(measure))
  const presets = [...input.presets].sort()
  const tier1 = [...input.tier1].sort((a, b) => a.code.localeCompare(b.code))

  const fromBuilder = (changes: Partial<BuilderState>): AnalysisRequest =>
    toRequest({ ...defaultBuilderState(years), ...changes }, years)

  const list: PrewarmedAnalysis[] = STARTER_EXAMPLES.map((starter) => ({
    name: `starter: ${starter.question}`,
    operation: starter.operation,
    spec: starter.spec(years, latest),
  }))
  const add = (name: string, operation: Operation, changes: Partial<BuilderState>) =>
    list.push({ name, operation, spec: fromBuilder({ ...changes, operation }) })

  for (const preset of presets) {
    for (const measure of measures) {
      add(`estimate: ${preset}, ${measure}, ${latest}`, 'estimate', {
        activity: { preset },
        measure,
      })
    }
  }
  for (const preset of presets) {
    add(`trend: ${preset}`, 'trend', { activity: { preset } })
  }
  for (const category of tier1) {
    add(`trend: ${category.code} ${category.name}`, 'trend', {
      activity: { include: [category.code], label: category.name },
    })
  }
  for (const category of tier1) {
    add(`estimate: ${category.code} ${category.name}, ${latest}`, 'estimate', {
      activity: { include: [category.code], label: category.name },
    })
  }
  const bySex = { groupA: { sex: 'male' }, groupB: { sex: 'female' } } as const
  for (const preset of presets) {
    add(`compare by sex: ${preset}`, 'compare', { activity: { preset }, ...bySex })
  }
  for (const category of tier1) {
    add(`compare by sex: ${category.code} ${category.name}`, 'compare', {
      activity: { include: [category.code], label: category.name },
      ...bySex,
    })
  }
  // 2020 alone, under its partial-year weights: what a trend's 2020 sends.
  if (input.weightSchemes.includes('pandemic') && years.includes(PARTIAL_YEAR)) {
    for (const preset of presets) {
      add(`pandemic weights: ${preset}, ${PARTIAL_YEAR}`, 'estimate', {
        activity: { preset },
        singleYear: PARTIAL_YEAR,
      })
    }
  }

  if (input.presets.includes(HOME_CHART.preset)) {
    for (const series of homeChartSpecs(years).series) {
      list.push({ name: `home chart: ${series.key}`, operation: 'trend', spec: series.spec })
      if (series.partial2020 !== null) {
        list.push({
          name: `home chart, 2020: ${series.key}`,
          operation: 'estimate',
          spec: series.partial2020,
        })
      }
    }
  }

  // "Adjust analysis" then Analyze, unchanged: the builder states every
  // default, so a starter comes back in other bytes. Estimate and trend
  // starters land on entries above; a comparison starter keeps its own
  // labels and would otherwise miss the snapshot (review round 2).
  for (const starter of STARTER_EXAMPLES) {
    list.push({
      name: `starter via the builder: ${starter.question}`,
      operation: starter.operation,
      spec: toRequest(
        fromRequest(starter.operation, starter.spec(years, latest), years),
        years,
      ),
    })
  }

  const seen = new Set<string>()
  return list.filter((item) => {
    const identity = `${item.operation} ${stableStringify(item.spec)}`
    if (seen.has(identity)) return false
    seen.add(identity)
    return true
  })
}
