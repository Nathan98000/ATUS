/**
 * Human-readable descriptions of analysis specifications and results.
 *
 * Everything here is presentation: deterministic templates over values the
 * API returned or the user chose. Nothing is computed analytically, and the
 * wording stays descriptive ("average time spent…"), never causal.
 */
import type {
  AnalysisRequest,
  CompareRequest,
  EstimateResponse,
  Operation,
  PopulationRequest,
} from '../api/types'
import {
  formatEstimate,
  formatPeople,
  formatProportionAsPercent,
  formatYears,
} from '../utils/format'

/**
 * Removes parentheticals that reference internal BLS variable names
 * (e.g. "(TRCHILDNUM > 0)", "(TELFS): …") from API descriptions before
 * showing them to nontechnical users. Presentation only — the full
 * descriptions remain available in the project documentation.
 */
export function stripVariableJargon(text: string): string {
  return text
    .replace(/\s*\([^)]*[A-Z]{4,}[^)]*\)/g, '')
    .replace(/\s{2,}/g, ' ')
    .trim()
}

/** "some_college_or_associate" → "Some college or associate". */
export function humanize(value: string): string {
  const spaced = value.replaceAll('_', ' ')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

const REGION_NAMES: Record<string, string> = {
  '1': 'Northeast',
  '2': 'Midwest',
  '3': 'South',
  '4': 'West',
}

const VALUE_LABELS: Record<string, Record<string, string>> = {
  sex: { male: 'Male', female: 'Female' },
  employment_status: {
    employed: 'Employed',
    unemployed: 'Unemployed',
    not_in_labor_force: 'Not in the labor force',
  },
  education_level: {
    less_than_high_school: 'Less than high school',
    high_school: 'High school',
    some_college_or_associate: 'Some college or associate degree',
    bachelor_or_higher: "Bachelor's degree or higher",
  },
  day_type: { weekday: 'Weekdays', weekend: 'Weekends' },
  region: REGION_NAMES,
}

/** Display label for a population dimension value (falls back to humanizing it). */
export function valueLabel(dimension: string, value: string | number | boolean): string {
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  const mapped = VALUE_LABELS[dimension]?.[String(value)]
  if (mapped !== undefined) return mapped
  return typeof value === 'string' ? humanize(value) : String(value)
}

export function dimensionLabel(name: string): string {
  const overrides: Record<string, string> = {
    age_min: 'Minimum age',
    age_max: 'Maximum age',
    sex: 'Sex',
    employment_status: 'Employment',
    has_household_children: 'Children in household',
    region: 'Region',
    state_fips: 'State (FIPS code)',
    education_level: 'Education',
    day_type: 'Day of week',
    diary_date_min: 'Diaries from',
    diary_date_max: 'Diaries through',
  }
  return overrides[name] ?? humanize(name)
}

/** Short phrase describing a population filter, e.g. "Women · Ages 25–54". */
export function populationPhrase(population: PopulationRequest | undefined): string {
  if (!population) return 'All respondents (age 15+)'
  const parts: string[] = []

  if (population.sex != null) parts.push(population.sex === 'female' ? 'Women' : 'Men')
  const { age_min, age_max } = population
  if (age_min != null && age_max != null) parts.push(`ages ${age_min}–${age_max}`)
  else if (age_min != null) parts.push(`age ${age_min}+`)
  else if (age_max != null) parts.push(`age ${age_max} or younger`)
  if (population.employment_status != null) {
    parts.push(valueLabel('employment_status', population.employment_status).toLowerCase())
  }
  if (population.education_level != null) {
    parts.push(valueLabel('education_level', population.education_level).toLowerCase())
  }
  if (population.has_household_children != null) {
    parts.push(
      population.has_household_children
        ? 'with household children'
        : 'without household children',
    )
  }
  if (population.region != null)
    parts.push(REGION_NAMES[String(population.region)] ?? `region ${population.region}`)
  if (population.state_fips != null) parts.push(`state FIPS ${population.state_fips}`)
  if (population.day_type != null) {
    parts.push(population.day_type === 'weekend' ? 'weekend days' : 'weekdays')
  }
  if (population.diary_date_min != null) parts.push(`from ${population.diary_date_min}`)
  if (population.diary_date_max != null) parts.push(`through ${population.diary_date_max}`)

  if (parts.length === 0) return 'All respondents (age 15+)'
  const first = parts[0] as string
  const capitalized = first.charAt(0).toUpperCase() + first.slice(1)
  return [capitalized, ...parts.slice(1)].join(' · ')
}

/** Display label for the spec's activity selection (before results arrive). */
export function activityLabelFromSpec(
  spec: AnalysisRequest,
  presetLabels?: ReadonlyMap<string, string>,
): string {
  const activity = spec.activity
  if (activity.preset != null) {
    return presetLabels?.get(activity.preset) ?? humanize(activity.preset)
  }
  if (activity.label != null && activity.label !== '') return activity.label
  const codes = activity.include ?? []
  if (codes.length === 1) return `Activity ${codes[0]}`
  return `Custom selection (${codes.length} codes)`
}

/** Page/result title, e.g. "Sleeping — Ages 25–54 — 2025". */
export function analysisTitle(
  operation: Operation,
  spec: AnalysisRequest,
  activityLabel: string,
): string {
  const parts = [activityLabel]
  if (operation === 'compare') {
    const compare = spec as CompareRequest
    parts.push(`${compare.label_a ?? 'Group A'} vs ${compare.label_b ?? 'Group B'}`)
  } else {
    parts.push(populationPhrase(spec.population))
  }
  parts.push(formatYears(spec.years))
  return parts.filter((part) => part !== '').join(' — ')
}

/**
 * The averaging clause must match what the weights actually average over:
 * a day_type filter restricts the estimate to weekend days or weekdays, and
 * a diary-date window restricts the period — never claim "all days of the
 * week" then.
 */
function averagingClause(population: PopulationRequest | undefined): string {
  const dayPhrase =
    population?.day_type === 'weekend'
      ? 'weekend days'
      : population?.day_type === 'weekday'
        ? 'weekdays'
        : 'all days of the week'
  const window =
    population?.diary_date_min != null || population?.diary_date_max != null
      ? ' within the selected diary period'
      : ''
  return `averaged across ${dayPhrase}${window}`
}

/**
 * A grammatical subject for the interpretation sentence. Filters that are
 * not person-nouns (state FIPS, diary dates, region alone) would read as
 * nonsense subjects ("State FIPS 06 spent…"), so those fall back to a
 * generic subject; the population is always fully described elsewhere on
 * the page.
 */
function interpretationSubject(population: PopulationRequest | undefined): string {
  const phrase = populationPhrase(population)
  if (phrase.startsWith('All respondents')) return 'Americans age 15 and over'
  if (/^(Women|Men|Employed|Unemployed|Not in the labor force)/.test(phrase)) return phrase
  if (/^Ages? /.test(phrase))
    return `People ${phrase.charAt(0).toLowerCase()}${phrase.slice(1)}`
  return 'People in the selected population'
}

/**
 * One deterministic plain-language sentence about an estimate. Descriptive
 * only — no causal claims, no significance language.
 */
export function interpretEstimate(result: EstimateResponse): string {
  const specPopulation = (result.spec as { population?: PopulationRequest }).population
  const population = populationPhrase(specPopulation)
  const activity = result.activity.label
  const years = formatYears(result.years)
  const { value, unit } = result.estimate

  switch (result.measure) {
    case 'average_minutes_per_day':
      return (
        `${interpretationSubject(specPopulation)} spent an estimated ` +
        `${formatEstimate(value, unit).primary} per day on “${activity}” in ${years}, ` +
        `${averagingClause(specPopulation)}.`
      )
    case 'participation_rate':
      return (
        `An estimated ${formatProportionAsPercent(value)} of the population ` +
        `(${population.toLowerCase()}) did “${activity}” on an average day in ${years}.`
      )
    case 'average_minutes_per_participant':
      return (
        `Those who did “${activity}” on a given day spent an estimated ` +
        `${formatEstimate(value, unit).primary} on it (${population.toLowerCase()}, ${years}).`
      )
    case 'participants_per_day':
      return (
        `An estimated ${formatPeople(value)} people (${population.toLowerCase()}) did ` +
        `“${activity}” on an average day in ${years}.`
      )
    default:
      return ''
  }
}
