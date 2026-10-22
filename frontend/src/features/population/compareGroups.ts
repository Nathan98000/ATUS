/**
 * "Compare by": a comparison of two groups that differ in one thing — sex,
 * an age band, employment, education, children at home, or weekdays vs
 * weekends. One choice and two values replace two full sets of filters;
 * anything else is a custom comparison with the full group editors. A
 * trend "split by" the same choices draws one line per group.
 *
 * Dimensions and values are offered only where the API's population
 * metadata reports them (the UI never invents filters). Group names are the
 * plain description of each group's filter (`populationPhrase`), so a result
 * reads "Men vs Women" and its small print has nothing to qualify.
 */
import type {
  AnalysisRequest,
  PopulationDimension,
  PopulationRequest,
  TrendRequest,
} from '../../api/types'
import { populationPhrase } from '../../domain/describe'
import type { SplitSelection } from '../../domain/urlSpec'

export type CompareBy =
  | 'sex'
  | 'age'
  | 'employment_status'
  | 'education_level'
  | 'has_household_children'
  | 'day_type'
  | 'custom'

/** Age bands offered for an age comparison (the oldest band is open-ended). */
export const AGE_BANDS: readonly { key: string; min: number; max: number | null }[] = [
  { key: '15-24', min: 15, max: 24 },
  { key: '25-34', min: 25, max: 34 },
  { key: '35-44', min: 35, max: 44 },
  { key: '45-54', min: 45, max: 54 },
  { key: '55-64', min: 55, max: 64 },
  { key: '65+', min: 65, max: null },
]

const LABELS: Record<Exclude<CompareBy, 'custom'>, string> = {
  sex: 'Sex',
  age: 'Age group',
  employment_status: 'Employment',
  education_level: 'Education',
  has_household_children: 'Children at home',
  day_type: 'Weekday or weekend',
}

/** Values in reading order (lowest to highest education, etc.), then defaults. */
const ORDER: Partial<Record<CompareBy, readonly string[]>> = {
  sex: ['male', 'female'],
  employment_status: ['employed', 'unemployed', 'not_in_labor_force'],
  education_level: [
    'less_than_high_school',
    'high_school',
    'some_college_or_associate',
    'bachelor_or_higher',
  ],
  has_household_children: ['true', 'false'],
  day_type: ['weekday', 'weekend'],
}

/** The pair a new comparison starts with: a contrast worth looking at. */
const DEFAULT_PAIR: Record<Exclude<CompareBy, 'custom'>, [string, string]> = {
  sex: ['male', 'female'],
  age: ['15-24', '65+'],
  employment_status: ['employed', 'not_in_labor_force'],
  education_level: ['bachelor_or_higher', 'high_school'],
  has_household_children: ['true', 'false'],
  day_type: ['weekday', 'weekend'],
}

export interface CompareOption {
  /** A string key: the value itself, 'true'/'false', or an age band key. */
  key: string
  label: string
  population: PopulationRequest
}

export interface CompareDimensionChoice {
  by: Exclude<CompareBy, 'custom'>
  label: string
  options: CompareOption[]
}

function makeOption(population: PopulationRequest, key: string): CompareOption {
  return { key, label: populationPhrase(population), population }
}

/** The "compare by" choices this API supports, each with its values. */
export function compareChoices(
  dimensions: readonly PopulationDimension[],
): CompareDimensionChoice[] {
  const byName = new Map(dimensions.map((dimension) => [dimension.name, dimension]))
  const choices: CompareDimensionChoice[] = []
  for (const by of Object.keys(LABELS) as Exclude<CompareBy, 'custom'>[]) {
    let options: CompareOption[] = []
    if (by === 'age') {
      if (!byName.has('age_min') || !byName.has('age_max')) continue
      options = AGE_BANDS.map((band) =>
        makeOption(
          band.max === null ? { age_min: band.min } : { age_min: band.min, age_max: band.max },
          band.key,
        ),
      )
    } else {
      const dimension = byName.get(by)
      if (!dimension) continue
      const values: (string | number | boolean)[] =
        dimension.type === 'boolean'
          ? [true, false]
          : ((dimension.values as (string | number)[] | null | undefined) ?? [])
      const order = ORDER[by] ?? []
      const rank = (value: string | number | boolean) => {
        const index = order.indexOf(String(value))
        return index === -1 ? order.length : index
      }
      options = [...values]
        .sort((a, b) => rank(a) - rank(b))
        .map((value) => makeOption({ [by]: value } as PopulationRequest, String(value)))
    }
    if (options.length >= 2) choices.push({ by, label: LABELS[by], options })
  }
  return choices
}

/** The default pair for a choice: its preferred contrast, else its first two values. */
export function defaultPair(choice: CompareDimensionChoice): [CompareOption, CompareOption] {
  const [a, b] = DEFAULT_PAIR[choice.by]
  const first = choice.options.find((candidate) => candidate.key === a) ?? choice.options[0]
  const second =
    choice.options.find((candidate) => candidate.key === b && candidate !== first) ??
    choice.options.find((candidate) => candidate !== first)
  return [first as CompareOption, second as CompareOption]
}

function samePopulation(a: PopulationRequest, b: PopulationRequest): boolean {
  const entries = (population: PopulationRequest) =>
    Object.entries(population)
      .filter(([, value]) => value != null)
      .sort(([x], [y]) => x.localeCompare(y))
  return JSON.stringify(entries(a)) === JSON.stringify(entries(b))
}

/**
 * Which simple comparison a pair of groups is, or 'custom'. Only an exact
 * match counts — both groups one value of the same choice, each named by
 * its plain description — so a link with other names or extra filters
 * opens in the custom editors, where everything it sets is visible.
 */
export function deriveCompareBy(
  choices: readonly CompareDimensionChoice[],
  groups: {
    groupA: PopulationRequest
    groupB: PopulationRequest
    labelA: string
    labelB: string
  },
): CompareBy {
  for (const choice of choices) {
    const a = choice.options.find((candidate) =>
      samePopulation(candidate.population, groups.groupA),
    )
    const b = choice.options.find((candidate) =>
      samePopulation(candidate.population, groups.groupB),
    )
    if (a && b && a !== b && a.label === groups.labelA && b.label === groups.labelB) {
      return choice.by
    }
  }
  return 'custom'
}

/** The population fields a choice sets (hidden from the other filters). */
export function choiceDimensions(by: CompareBy): string[] {
  if (by === 'custom') return []
  return by === 'age' ? ['age_min', 'age_max'] : [by]
}

/** A split's choice and its groups, in the choice's order. */
export interface ResolvedSplit {
  choice: CompareDimensionChoice
  options: CompareOption[]
}

/**
 * The split a link asks for, against this release's metadata: null when the
 * dimension or any group is not offered (the link is then unusable here).
 */
export function resolveSplit(
  dimensions: readonly PopulationDimension[],
  split: SplitSelection,
): ResolvedSplit | null {
  const choice = compareChoices(dimensions).find((candidate) => candidate.by === split.by)
  if (!choice) return null
  if (!split.groups.every((key) => choice.options.some((option) => option.key === key))) {
    return null
  }
  return {
    choice,
    options: choice.options.filter((option) => split.groups.includes(option.key)),
  }
}

/**
 * One group's line of a split trend: the trend's own population with the
 * group's filter in place of any on the same dimension, as point estimates
 * (a chart of several lines draws no bands, and point estimates take about
 * a tenth of the time).
 */
export function seriesSpec(
  spec: AnalysisRequest,
  split: ResolvedSplit,
  option: CompareOption,
): TrendRequest {
  const population = { ...spec.population } as Record<string, unknown>
  for (const name of choiceDimensions(split.choice.by)) delete population[name]
  return {
    ...spec,
    population: { ...population, ...option.population } as PopulationRequest,
    variance: 'none',
  }
}
