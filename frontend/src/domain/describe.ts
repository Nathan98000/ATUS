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
  CompareResponse,
  EstimateResponse,
  EstimateValue,
  Operation,
  PopulationRequest,
  TrendResponse,
} from '../api/types'
import {
  formatConfidenceLevel,
  formatDurationWords,
  formatEstimate,
  formatMinutesDetailed,
  formatPeople,
  formatPercentagePoints,
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

const ALL_RESPONDENTS = 'All respondents (age 15+)'

/** Short phrase describing a population filter, e.g. "Women · Ages 25–54". */
export function populationPhrase(population: PopulationRequest | undefined): string {
  if (!population) return ALL_RESPONDENTS
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

  if (parts.length === 0) return ALL_RESPONDENTS
  const first = parts[0] as string
  const capitalized = first.charAt(0).toUpperCase() + first.slice(1)
  return [capitalized, ...parts.slice(1)].join(' · ')
}

/**
 * A comparison's shared population — it restricts both groups — or null when
 * it restricts nothing. The result page names it outside its collapsed
 * panels (review round 3).
 */
export function sharedPopulationPhrase(spec: AnalysisRequest): string | null {
  const phrase = populationPhrase(spec.population)
  return phrase === ALL_RESPONDENTS ? null : phrase
}

/**
 * Labels a share link chose, set against what was computed (R14 in
 * docs/security.md). A comparison's group names are free text, so when a
 * name is not the plain description of its group's filters the note says
 * which filters it stands for; a custom activity's name is free text too,
 * so the note names its activity codes — unless the selection is exactly a
 * preset under the preset's own name (the canonical form of a preset's
 * link). Null when there is nothing to add.
 */
export function definitionNote(
  operation: Operation,
  spec: AnalysisRequest,
  presets: readonly { label: string; include: string[]; exclude: string[] }[] = [],
): string | null {
  const parts: string[] = []
  if (operation === 'compare') {
    const compare = spec as CompareRequest
    const groups = [
      { label: compare.label_a ?? 'Group A', phrase: populationPhrase(compare.group_a) },
      { label: compare.label_b ?? 'Group B', phrase: populationPhrase(compare.group_b) },
    ]
    if (groups.some((group) => group.label !== group.phrase)) {
      parts.push(
        `Groups compared: ${groups.map((group) => `${group.label} (${group.phrase.toLowerCase()})`).join(' and ')}.`,
      )
    }
  }
  const activity = spec.activity
  const sameCodes = (a: readonly string[], b: readonly string[]) =>
    a.length === b.length && [...a].sort().join() === [...b].sort().join()
  const isPreset = presets.some(
    (preset) =>
      preset.label === activity.label &&
      sameCodes(preset.include, activity.include ?? []) &&
      sameCodes(preset.exclude, activity.exclude ?? []),
  )
  if (activity.preset == null && !isPreset && (activity.include?.length ?? 0) > 0) {
    const codes = activity.include ?? []
    let text = `Activity ${codes.length === 1 ? 'code' : 'codes'} ${codes.join(', ')}`
    if (activity.exclude && activity.exclude.length > 0) {
      text += `, excluding ${activity.exclude.join(', ')}`
    }
    parts.push(`${text}.`)
  }
  return parts.length > 0 ? parts.join(' ') : null
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

/**
 * Page/result title, e.g. "Sleeping — Ages 25–54 — 2025"; for a comparison
 * "Sleeping — Men vs Women — 2025", plus its shared population when one
 * restricts both groups ("… — Men vs Women — Age 65+ — 2025", review round 3).
 */
export function analysisTitle(
  operation: Operation,
  spec: AnalysisRequest,
  activityLabel: string,
): string {
  const parts = [activityLabel]
  if (operation === 'compare') {
    const compare = spec as CompareRequest
    parts.push(`${compare.label_a ?? 'Group A'} vs ${compare.label_b ?? 'Group B'}`)
    const shared = sharedPopulationPhrase(spec)
    if (shared !== null) parts.push(shared)
  } else {
    parts.push(populationPhrase(spec.population))
  }
  parts.push(formatYears(spec.years))
  return parts.filter((part) => part !== '').join(' — ')
}

const MEASURE_NAMES: Record<string, string> = {
  average_minutes_per_day: 'Average time per day',
  participation_rate: 'Share of people who do it',
  average_minutes_per_participant: 'Time among those who do it',
  participants_per_day: 'Number of people who do it',
}

/** Display name of a statistic, e.g. "Average time per day". */
export function measureName(measure: string): string {
  return MEASURE_NAMES[measure] ?? humanize(measure)
}

/**
 * Splits a definition qualifier off an activity label for display:
 * "Leisure and sports (BLS table A-1 definition)" → name "Leisure and
 * sports", qualifier "As defined in BLS table A-1". The qualifier must stay
 * on the page: two presets can share a name and differ only in it.
 */
export function splitActivityLabel(label: string): { name: string; qualifier: string | null } {
  const match = label.match(/^(.+?)\s*\((BLS table [^)]*?)\s+definition\)\s*$/i)
  if (!match) return { name: label, qualifier: null }
  return { name: match[1] as string, qualifier: `As defined in ${match[2] as string}` }
}

export interface ResultHeading {
  /** The statistic, e.g. "Average time per day" or "…, by year". */
  eyebrow: string
  /** The activity (and, for a comparison, the two groups). */
  title: string
  /** Everything else, said once: definition, population, years. */
  subtitle: string
}

/**
 * A result page's heading: what was measured, for what, for whom, when —
 * each said once, without the activity's definition parenthetical in the
 * title itself.
 */
export function resultHeading(
  operation: Operation,
  spec: AnalysisRequest,
  activityLabel: string,
): ResultHeading {
  const { name, qualifier } = splitActivityLabel(activityLabel)
  const measure = measureName(spec.measure ?? 'average_minutes_per_day')
  const details = qualifier !== null ? [qualifier] : []
  let title = name
  if (operation === 'compare') {
    const compare = spec as CompareRequest
    title = `${name}: ${compare.label_a ?? 'Group A'} vs ${compare.label_b ?? 'Group B'}`
    const shared = sharedPopulationPhrase(spec)
    if (shared !== null) details.push(`Both groups: ${shared}`)
  } else {
    details.push(populationPhrase(spec.population))
  }
  details.push(formatYears(spec.years))
  return {
    eyebrow: operation === 'trend' ? `${measure}, by year` : measure,
    title,
    subtitle: details.filter((detail) => detail !== '').join(' · '),
  }
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

/** "Average time per day on “Sleeping”", "The share of people doing “Sleeping”…", or null. */
function measureSubject(measure: string, activity: string): string | null {
  switch (measure) {
    case 'average_minutes_per_day':
      return `Average time per day on ${activity}`
    case 'participation_rate':
      return `The share of people doing ${activity} on an average day`
    case 'average_minutes_per_participant':
      return `Time per day among those who did ${activity}`
    case 'participants_per_day':
      return `The number of people doing ${activity} on an average day`
    default:
      return null
  }
}

/**
 * The plain-language answer a trend leads with: the latest year, the first
 * year, and the range in between. Descriptive only: it names values the API
 * returned and makes no claim that anything rose or fell.
 */
export function interpretTrend(result: TrendResponse): string {
  const available = result.points.flatMap((point) =>
    point.estimate != null ? [{ year: point.year, estimate: point.estimate }] : [],
  )
  const first = available[0]
  const last = available[available.length - 1]
  if (first === undefined || last === undefined) return ''
  const activity = `“${splitActivityLabel(result.activity.label).name}”`
  const subject = measureSubject(result.measure, activity)
  if (subject === null) return ''

  const specPopulation = (result.spec as { population?: PopulationRequest }).population
  const population = populationPhrase(specPopulation)
  const who = population === ALL_RESPONDENTS ? '' : ` (${population})`
  const show = (estimate: EstimateValue) =>
    formatEstimate(estimate.value, estimate.unit).primary

  let text = `${subject}${who} was ${show(last.estimate)} in ${last.year}`
  if (available.length > 1) text += `, compared with ${show(first.estimate)} in ${first.year}`
  text += '.'
  if (available.length >= 3) {
    let low = first
    let high = first
    for (const point of available) {
      if (point.estimate.value < low.estimate.value) low = point
      if (point.estimate.value > high.estimate.value) high = point
    }
    text +=
      show(low.estimate) === show(high.estimate)
        ? ` It stayed at about ${show(low.estimate)} throughout.`
        : ` Over these years it ranged from ${show(low.estimate)} in ${low.year} to ${show(high.estimate)} in ${high.year}.`
  }
  return text
}

/**
 * The answer a split trend leads with: each group's value in the latest
 * year every group has one — named in turn for two or three groups, as the
 * highest and the lowest for more. Descriptive only.
 */
export function interpretSplitTrend(
  series: readonly { label: string; result: TrendResponse }[],
): string {
  const first = series[0]?.result
  if (first === undefined) return ''
  const activity = `“${splitActivityLabel(first.activity.label).name}”`
  const subject = measureSubject(first.measure, activity)
  if (subject === null) return ''
  const years = first.points.map((point) => point.year).sort((a, b) => b - a)
  for (const year of years) {
    const values = series.map(({ label, result }) => ({
      label,
      estimate: result.points.find((point) => point.year === year)?.estimate ?? null,
    }))
    if (values.some((value) => value.estimate == null)) continue
    const show = ({ estimate }: (typeof values)[number]) =>
      formatEstimate((estimate as EstimateValue).value, (estimate as EstimateValue).unit)
        .primary
    if (values.length <= 3) {
      const parts = values.map((value) => `${show(value)} for ${value.label}`)
      const listed =
        parts.length === 2
          ? parts.join(' and ')
          : `${parts.slice(0, -1).join(', ')}, and ${parts.at(-1)}`
      return `In ${year}, ${lowerFirst(subject)} was ${listed}.`
    }
    const sorted = [...values].sort(
      (a, b) => (b.estimate as EstimateValue).value - (a.estimate as EstimateValue).value,
    )
    const high = sorted[0] as (typeof values)[number]
    const low = sorted[sorted.length - 1] as (typeof values)[number]
    return `In ${year}, ${lowerFirst(subject)} was highest for ${high.label} (${show(high)}) and lowest for ${low.label} (${show(low)}).`
  }
  return ''
}

export interface DifferenceDescription {
  /** The group whose estimate is larger, or null when they are equal. */
  larger: 'a' | 'b' | null
  /** "23 minutes more per day", "6.2 percentage points higher", … */
  amount: string
  /** "Likely range: 13.8 to 32.5 min more for Men (95% confidence)", or null without one. */
  interval: string | null
  /** The one-sentence answer a comparison leads with. */
  sentence: string
}

/**
 * A comparison in words, from the API's difference (group A − group B): who
 * the estimate is larger for, by how much, and the confidence interval as a
 * range of "more for A / more for B". Descriptive only — no causal or
 * significance language. Group labels are any noun phrase a visitor typed,
 * so they only ever follow "for".
 */
export function describeDifference(result: CompareResponse): DifferenceDescription {
  const { difference, label_a: labelA, label_b: labelB } = result
  const unit = difference.unit
  const larger = difference.value > 0 ? 'a' : difference.value < 0 ? 'b' : null
  const largerLabel = larger === 'b' ? labelB : labelA
  const smallerLabel = larger === 'b' ? labelA : labelB

  const isTime = unit === 'minutes_per_day' || unit === 'minutes_per_day_of_participants'
  const isShare = unit === 'proportion_of_population'
  const word = isShare ? 'higher' : 'more'
  const magnitude = (value: number): string =>
    isTime
      ? formatDurationWords(value)
      : isShare
        ? formatPercentagePoints(Math.abs(value))
        : formatPeople(Math.abs(value))
  const bound = (value: number): string =>
    isTime
      ? `${formatMinutesDetailed(Math.abs(value))} min`
      : isShare
        ? formatPercentagePoints(Math.abs(value))
        : formatPeople(Math.abs(value))
  // "13.8 to 32.5 min": the unit once, after the second bound.
  const range = (from: number, to: number): string =>
    isTime
      ? `${formatMinutesDetailed(Math.abs(from))} to ${bound(to)}`
      : isShare
        ? `${formatPercentagePoints(Math.abs(from), false)} to ${bound(to)}`
        : `${bound(from)} to ${bound(to)}`

  const amount =
    larger === null
      ? 'No difference'
      : `${magnitude(difference.value)} ${word}${isTime ? ' per day' : ''}`

  let interval: string | null = null
  const { ci_lower: lower, ci_upper: upper, confidence_level: level } = difference
  if (lower != null && upper != null && level != null) {
    const prefix = 'Likely range: '
    const suffix = ` (${formatConfidenceLevel(level)} confidence)`
    if (lower >= 0) {
      interval = `${prefix}${range(lower, upper)} ${word} for ${labelA}${suffix}`
    } else if (upper <= 0) {
      interval = `${prefix}${range(upper, lower)} ${word} for ${labelB}${suffix}`
    } else {
      interval = `${prefix}from ${bound(lower)} ${word} for ${labelB} to ${bound(upper)} ${word} for ${labelA}${suffix}`
    }
  }

  const activity = `“${splitActivityLabel(result.group_a.activity.label).name}”`
  const subject = measureSubject(result.measure, activity) ?? measureName(result.measure)
  const years = result.group_a.years
  const when =
    years.length === 1 ? `In ${formatYears(years)}` : `Across ${formatYears(years)} combined`
  const comparative = isTime ? 'longer' : 'higher'
  const sentence =
    larger === null
      ? `${when}, ${lowerFirst(subject)} was the same for ${labelA} and for ${labelB}.`
      : `${when}, ${lowerFirst(subject)} was ${magnitude(difference.value)} ${comparative} for ${largerLabel} than for ${smallerLabel}.`
  return { larger, amount, interval, sentence }
}

/** "Average time…" → "average time…" (only the sentence's own first word). */
function lowerFirst(text: string): string {
  return text.charAt(0).toLowerCase() + text.slice(1)
}
