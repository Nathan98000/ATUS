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
  confidenceIntervalName,
  formatCount,
  formatDurationWords,
  formatEstimate,
  formatMinutesDetailed,
  formatPeople,
  formatPercentagePoints,
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
    has_household_children: 'Children under 18 at home',
    region: 'Region',
    state_fips: 'State (FIPS code)',
    education_level: 'Education',
    day_type: 'Day of week',
    diary_date_min: 'Diaries from',
    diary_date_max: 'Diaries through',
  }
  return overrides[name] ?? humanize(name)
}

/** No filter at all, in the words the builder uses (review round 4). */
const EVERYONE = 'Everyone age 15 and over'

/** Short phrase describing a population filter, e.g. "Women · Ages 25–54". */
export function populationPhrase(population: PopulationRequest | undefined): string {
  if (!population) return EVERYONE
  const parts: string[] = []

  if (population.sex != null) parts.push(population.sex === 'female' ? 'Women' : 'Men')
  const { age_min, age_max } = population
  if (age_min != null && age_max != null) parts.push(`ages ${age_min}–${age_max}`)
  else if (age_min != null) parts.push(`ages ${age_min} and over`)
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

  if (parts.length === 0) return EVERYONE
  return upperFirst(parts.join(' · '))
}

const EDUCATION_PHRASES: Record<string, string> = {
  less_than_high_school: 'without a high school diploma',
  high_school: 'with a high school diploma and no college',
  some_college_or_associate: 'with some college or an associate degree',
  bachelor_or_higher: "with a bachelor's degree or higher",
}

/**
 * The population as the people a sentence talks about: "Americans age 15
 * and over", "women ages 25–54", "employed men with children under 18 at
 * home", "people with a bachelor's degree or higher" (review round 4: the
 * heading form, "Women · Ages 25–54", read badly inside a sentence). Day of
 * week and diary dates are not people; `dayPhrase` and the averaging clause
 * say those.
 */
export function peoplePhrase(population: PopulationRequest | undefined): string {
  const p = population ?? {}
  const before: string[] = []
  const after: string[] = []
  const clauses: string[] = []
  if (p.employment_status === 'employed' || p.employment_status === 'unemployed') {
    before.push(p.employment_status)
  } else if (p.employment_status != null) {
    after.push(valueLabel('employment_status', p.employment_status).toLowerCase())
  }
  const noun = p.sex === 'female' ? 'women' : p.sex === 'male' ? 'men' : 'people'
  if (p.age_min != null && p.age_max != null) after.unshift(`ages ${p.age_min}–${p.age_max}`)
  else if (p.age_min != null) after.unshift(`ages ${p.age_min} and over`)
  else if (p.age_max != null) after.unshift(`age ${p.age_max} or younger`)
  if (p.education_level != null) {
    clauses.push(
      EDUCATION_PHRASES[p.education_level] ??
        `with education: ${valueLabel('education_level', p.education_level).toLowerCase()}`,
    )
  }
  if (p.has_household_children != null) {
    clauses.push(
      p.has_household_children
        ? 'with children under 18 at home'
        : 'without children under 18 at home',
    )
  }
  if (p.region != null) {
    const region = REGION_NAMES[String(p.region)]
    after.push(region ? `in the ${region}` : `in region ${p.region}`)
  }
  if (p.state_fips != null) after.push(`in state FIPS ${p.state_fips}`)
  if (before.length === 0 && noun === 'people' && after.length === 0 && clauses.length === 0) {
    return 'Americans age 15 and over'
  }
  return [...before, noun, ...after, clauses.join(' and ')].filter(Boolean).join(' ')
}

/** "a day", or "per weekday" / "per weekend day", and the day it describes. */
function dayPhrase(population: PopulationRequest | undefined): {
  per: string
  average: string
  given: string
} {
  if (population?.day_type === 'weekend') {
    return {
      per: 'per weekend day',
      average: 'an average weekend day',
      given: 'a given weekend day',
    }
  }
  if (population?.day_type === 'weekday') {
    return { per: 'per weekday', average: 'an average weekday', given: 'a given weekday' }
  }
  return { per: 'a day', average: 'an average day', given: 'a given day' }
}

/**
 * The activity inside a sentence, after "spent time": "sleeping", "watching
 * TV", "on household activities". A name that starts with an -ing word
 * reads without "on". No quotation marks (review round 4).
 */
function activityPhrase(label: string): string {
  const name = lowerFirst(activityName(label))
  return /^[a-z]+ing\b/.test(name) ? name : `on ${name}`
}

/**
 * A comparison's shared population — it restricts both groups — or null when
 * it restricts nothing. The result page names it outside its collapsed
 * panels (review round 3).
 */
export function sharedPopulationPhrase(spec: AnalysisRequest): string | null {
  const phrase = populationPhrase(spec.population)
  return phrase === EVERYONE ? null : phrase
}

/**
 * Labels a share link chose, set against what was computed (R14 in
 * docs/security.md). A comparison's group names are free text, so when a
 * name is not the plain description of its group's filters the note says
 * which filters it stands for. An activity's name is free text too, so
 * the note names its activity codes when the name is neither a preset's
 * own (the selection exactly that preset) nor the survey's own name for
 * its one activity code (`surveyNames`, code → name). The builder writes
 * only those, so its links never carry this note (review round 5); while
 * the survey's names are unknown, nothing is said about the activity.
 * Null when there is nothing to add.
 */
export function definitionNote(
  operation: Operation,
  spec: AnalysisRequest,
  presets: readonly { label: string; include: string[]; exclude: string[] }[] = [],
  surveyNames?: ReadonlyMap<string, string>,
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
  const codes = activity.include ?? []
  const surveyName =
    codes.length === 1 && (activity.exclude?.length ?? 0) === 0
      ? surveyNames?.get(codes[0] as string)
      : undefined
  if (
    activity.preset == null &&
    !isPreset &&
    codes.length > 0 &&
    activity.label != null &&
    activity.label !== '' &&
    surveyNames !== undefined &&
    activity.label !== surveyName
  ) {
    // In words, with the codes themselves (review round 4: "Activity code
    // 15." alone meant nothing to a reader).
    const name = `“${activityName(activity.label)}”`
    // A two- or four-digit code is a category: it includes every activity
    // under it.
    const broad = codes.some((code) => code.length < 6)
    let text = `${name} here means survey activity ${codes.length === 1 ? 'code' : 'codes'} ${codes.join(', ')}`
    if (broad) text += ` and the activities under ${codes.length === 1 ? 'it' : 'them'}`
    if (activity.exclude && activity.exclude.length > 0) {
      text += `, except ${activity.exclude.join(', ')}`
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
  const parts = [activityName(activityLabel)]
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
 * sports", qualifier "As defined in BLS table A-1". The site shows the
 * name only; where two presets would share one, the pickers offer just the
 * BLS-table one (`offeredPresets`).
 */
export function splitActivityLabel(label: string): { name: string; qualifier: string | null } {
  const match = label.match(/^(.+?)\s*\((BLS table [^)]*?)\s+definition\)\s*$/i)
  if (!match) return { name: label, qualifier: null }
  return { name: match[1] as string, qualifier: `As defined in ${match[2] as string}` }
}

/**
 * Plain names for the survey's top-level categories whose own names are
 * hard going ("Caring For & Helping Household (HH) Members"), keyed by the
 * survey's exact name: a name the survey changes falls back to the general
 * clean-up below.
 */
const PLAIN_CATEGORY_NAMES: Record<string, string> = {
  'Personal Care Activities': 'Personal care, including sleep',
  'Caring For & Helping Household (HH) Members': 'Caring for household members',
  'Caring For & Helping Nonhousehold (NonHH) Members': 'Helping people outside the household',
  'Consumer Purchases': 'Shopping',
  'Professional & Personal Care Services': 'Using professional and personal services',
  'Household Services': 'Using household services',
  'Government Services & Civic Obligations': 'Government services and civic duties',
  'Volunteer Activities': 'Volunteering',
  'Telephone Calls': 'Phone calls',
}

/**
 * The survey's activity names read as plain words: "HH" spelled out, "&"
 * as "and", Title Case as sentence case ("Activities Related to HH
 * Children's Education" → "Activities related to household children's
 * education"). Words in capitals ("TV") keep them. Only letters' case and
 * these abbreviations change, never the words (review round 4).
 */
export function plainActivityName(name: string): string {
  const plain = PLAIN_CATEGORY_NAMES[name]
  if (plain !== undefined) return plain
  const text = name
    .replace(/\s*\((?:HH|NonHH)\)/g, '')
    .replace(/\bnon-?hh\b/gi, 'non-household')
    .replace(/\bhh\b/gi, 'household')
    .replace(/\s+&\s+/g, ' and ')
    .replace(/\bw\/\s*/g, 'with ')
  const [first = '', ...rest] = text.split(' ')
  // Title Case: a later word starts with a capital followed by lowercase.
  if (!rest.some((word) => /^\(?\p{Lu}\p{Ll}/u.test(word))) return upperFirst(text)
  const lowered = rest.map((word) =>
    word.replace(
      /(^|[(/-])(\p{Lu})(?=\p{Ll})/gu,
      (_, before: string, letter: string) => `${before}${letter.toLowerCase()}`,
    ),
  )
  return upperFirst([first, ...lowered].join(' '))
}

/**
 * An activity's name as the site shows it: without a definition qualifier,
 * in plain words (`plainActivityName`).
 */
export function activityName(label: string): string {
  return plainActivityName(splitActivityLabel(label).name)
}

/**
 * The presets a picker offers: each under its plain name and, where two
 * share one ("Eating and drinking"), only the one as defined in the BLS
 * published tables, so no two choices read the same.
 */
export function offeredPresets<T extends { label: string }>(presets: readonly T[]): T[] {
  const byName = new Map<string, T>()
  for (const preset of presets) {
    const name = activityName(preset.label)
    const kept = byName.get(name)
    const official = (label: string) => splitActivityLabel(label).qualifier !== null
    if (kept === undefined || (official(preset.label) && !official(kept.label))) {
      byName.set(name, preset)
    }
  }
  return presets.filter((preset) => byName.get(activityName(preset.label)) === preset)
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
  const name = activityName(activityLabel)
  const measure = measureName(spec.measure ?? 'average_minutes_per_day')
  const details: string[] = []
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
 * a day_type filter restricts the estimate to weekend days or weekdays
 * (`dayPhrase` then says "per weekend day"), and a diary-date window
 * restricts the period — never claim "all days of the week" then.
 */
function averagingClause(population: PopulationRequest | undefined): string {
  const window =
    population?.diary_date_min != null || population?.diary_date_max != null
      ? ' within the selected diary period'
      : ''
  if (population?.day_type != null) return window === '' ? '' : `,${window}`
  return `, averaged across all days of the week${window}`
}

function isDuration(unit: string): boolean {
  return unit === 'minutes_per_day' || unit === 'minutes_per_day_of_participants'
}

/** A value inside a sentence: a duration in words ("9 hours 2 minutes"), anything else as shown on the page. */
function proseValue(estimate: EstimateValue): string {
  return isDuration(estimate.unit)
    ? formatDurationWords(estimate.value)
    : formatEstimate(estimate.value, estimate.unit).primary
}

/**
 * What a measure says about people, up to its value: "Americans age 15 and
 * over spent ⟨9 hours 2 minutes⟩ a day sleeping", "the share of women who
 * spent time volunteering on an average day was ⟨5.2%⟩" — lowercase but
 * for a proper noun, to start a sentence or follow "In 2025,". Null for a
 * measure this module does not know.
 */
function lead(
  measure: string,
  who: string,
  day: ReturnType<typeof dayPhrase>,
  activity: string,
): ((value: string) => string) | null {
  switch (measure) {
    case 'average_minutes_per_day':
      return (value) => `${who} spent ${value} ${day.per} ${activity}`
    case 'participation_rate':
      return (value) =>
        `the share of ${who} who spent time ${activity} on ${day.average} was ${value}`
    case 'average_minutes_per_participant':
      return (value) =>
        `${who} who spent any time ${activity} on ${day.given} spent ${value} on it`
    case 'participants_per_day':
      return (value) =>
        `the number of ${who} who spent time ${activity} on ${day.average} was ${value}`
    default:
      return null
  }
}

/**
 * One deterministic plain-language sentence about an estimate. Descriptive
 * only — no causal claims, no significance language.
 */
export function interpretEstimate(result: EstimateResponse): string {
  const population = (result.spec as { population?: PopulationRequest }).population
  const sentence = lead(
    result.measure,
    peoplePhrase(population),
    dayPhrase(population),
    activityPhrase(result.activity.label),
  )
  if (sentence === null) return ''
  const clause = result.measure === 'average_minutes_per_day' ? averagingClause(population) : ''
  return `${upperFirst(sentence(`an estimated ${proseValue(result.estimate)}`))} in ${formatYears(result.years)}${clause}.`
}

/** "average time a day spent sleeping"-style subject for a sentence about groups, or null. */
function measureSubject(
  measure: string,
  activity: string,
  day: ReturnType<typeof dayPhrase>,
): string | null {
  switch (measure) {
    case 'average_minutes_per_day':
      return `average time ${day.per} spent ${activity}`
    case 'participation_rate':
      return `the share of people who spent time ${activity} on ${day.average}`
    case 'average_minutes_per_participant':
      return `time spent ${activity} among those who did it`
    case 'participants_per_day':
      return `the number of people who spent time ${activity} on ${day.average}`
    default:
      return null
  }
}

/**
 * The plain-language answer a trend leads with: the latest year, the first
 * year, and the range in between. Descriptive only: it names values the API
 * returned and makes no claim that anything rose or fell. `partialYears`:
 * years the chart draws from a partial-year estimate (2020); the answer
 * reads only the full-year points, so with one it names no range.
 */
export function interpretTrend(
  result: TrendResponse,
  partialYears: readonly number[] = [],
): string {
  const available = result.points.flatMap((point) =>
    point.estimate != null ? [{ year: point.year, estimate: point.estimate }] : [],
  )
  const first = available[0]
  const last = available[available.length - 1]
  if (first === undefined || last === undefined) return ''
  const population = (result.spec as { population?: PopulationRequest }).population
  const sentence = lead(
    result.measure,
    peoplePhrase(population),
    dayPhrase(population),
    activityPhrase(result.activity.label),
  )
  if (sentence === null) return ''

  let text = `${upperFirst(sentence(proseValue(last.estimate)))} in ${last.year}`
  if (available.length > 1)
    text += `, compared with ${proseValue(first.estimate)} in ${first.year}`
  text += '.'
  // The range across the years reads the trend's full-year points only, so
  // a chart with a partial year (2020) gives no range rather than one that
  // leaves a drawn point out (review round 5).
  if (available.length >= 3 && partialYears.length === 0) {
    let low = first
    let high = first
    for (const point of available) {
      if (point.estimate.value < low.estimate.value) low = point
      if (point.estimate.value > high.estimate.value) high = point
    }
    const over = 'Over these years it'
    const show = (estimate: EstimateValue) =>
      formatEstimate(estimate.value, estimate.unit).primary
    text +=
      show(low.estimate) === show(high.estimate)
        ? ` ${over} stayed at about ${proseValue(low.estimate)} throughout.`
        : ` ${over} ranged from ${proseValue(low.estimate)} in ${low.year} to ${proseValue(high.estimate)} in ${high.year}.`
  }
  return text
}

/** "a, b, and c" / "a and b" / "a". */
function listed(parts: readonly string[]): string {
  if (parts.length <= 1) return parts.join('')
  if (parts.length === 2) return `${parts[0]}, and ${parts[1]}`
  return `${parts.slice(0, -1).join(', ')}, and ${parts.at(-1)}`
}

/**
 * The answer a split trend leads with: each group's value in the latest
 * year every group has one — named in turn for two or three groups, as the
 * highest and the lowest for more. Built-in groups (`filters`: the group's
 * own filter, whose plain description is its label) are named as people
 * ("men", "people ages 25–34") or, for weekdays and weekend days, as days;
 * any other label follows "for", as written. Descriptive only.
 */
export function interpretSplitTrend(
  series: readonly { label: string; result: TrendResponse; filters?: PopulationRequest }[],
): string {
  const first = series[0]?.result
  if (first === undefined) return ''
  const activity = activityPhrase(first.activity.label)
  const groups = series.map(({ label, result, filters }) => {
    // The group's filter is in its own trend's spec; it is added again so
    // the sentence never names a group by less than its filter.
    const population = {
      ...(result.spec as { population?: PopulationRequest }).population,
      ...filters,
    }
    const builtIn = filters !== undefined && label === populationPhrase(filters)
    return {
      result,
      builtIn,
      byDay: filters?.day_type != null,
      who: peoplePhrase(population),
      day: dayPhrase(population),
      name: !builtIn
        ? label
        : filters?.day_type != null
          ? lowerFirst(label)
          : peoplePhrase(population),
    }
  })
  const natural = groups.every((group) => group.builtIn)
  const years = first.points.map((point) => point.year).sort((a, b) => b - a)
  for (const year of years) {
    const values = groups.map((group) => ({
      ...group,
      estimate: group.result.points.find((point) => point.year === year)?.estimate ?? null,
    }))
    if (values.some((value) => value.estimate == null)) continue
    const show = (value: (typeof values)[number]) => proseValue(value.estimate as EstimateValue)
    const [head, ...rest] = values
    if (head === undefined) return ''
    const opening = lead(first.measure, head.who, head.day, activity)
    const subject = measureSubject(first.measure, activity, head.day)
    if (opening === null || subject === null) return ''
    if (values.length > 3) {
      const sorted = [...values].sort(
        (a, b) => (b.estimate as EstimateValue).value - (a.estimate as EstimateValue).value,
      )
      const high = sorted[0] as (typeof values)[number]
      const low = sorted[sorted.length - 1] as (typeof values)[number]
      return `In ${year}, ${subject} was highest for ${high.name} (${show(high)}) and lowest for ${low.name} (${show(low)}).`
    }
    if (!natural) {
      const parts = values.map((value) => `${show(value)} for ${value.name}`)
      const joined = parts.length === 2 ? parts.join(' and ') : listed(parts)
      return `In ${year}, ${subject} was ${joined}.`
    }
    const perDay = first.measure === 'average_minutes_per_day'
    const others = rest.map((value) => {
      if (value.byDay) {
        return perDay
          ? `${show(value)} ${value.day.per}`
          : `on ${first.measure === 'average_minutes_per_participant' ? value.day.given : value.day.average} ${show(value)}`
      }
      return first.measure === 'participation_rate' || first.measure === 'participants_per_day'
        ? `of ${value.who} ${show(value)}`
        : `${value.who} ${show(value)}`
    })
    return `In ${year}, ${listed([opening(show(head)), ...others])}.`
  }
  return ''
}

export interface DifferenceDescription {
  /** The group whose estimate is larger, or null when they are equal. */
  larger: 'a' | 'b' | null
  /** The larger group as a sentence names it ("men", "weekend days", or a link's own label). */
  largerName: string | null
  /** "23 minutes more per day", "6.2 percentage points higher", … */
  amount: string
  /** "95% confidence interval: 14 to 33 minutes more for men", or null without one. */
  interval: string | null
  /** Whether the difference's confidence interval includes zero. */
  includesZero: boolean
  /** The one-sentence answer a comparison leads with. */
  sentence: string
}

/** A comparison's group as its sentences name it. */
interface NamedGroup {
  /** The people ("men"), or null for a link's own label. */
  who: string | null
  day: ReturnType<typeof dayPhrase>
  /** After "for": "men", "weekdays", or the label. */
  name: string
}

function namedGroup(
  label: string,
  groupFilters: PopulationRequest | undefined,
  population: PopulationRequest | undefined,
): NamedGroup {
  const day = dayPhrase(population)
  // A built-in group's label is the plain description of its own filters;
  // any other label is a link's or a visitor's own, kept as written (R14).
  if (groupFilters === undefined || label !== populationPhrase(groupFilters)) {
    return { who: null, day, name: label }
  }
  const who = peoplePhrase(population)
  return { who, day, name: groupFilters.day_type != null ? lowerFirst(label) : who }
}

/**
 * A comparison in words, from the API's difference (group A − group B): who
 * the estimate is larger for, by how much, and the confidence interval as a
 * range of "more for A / more for B". Built-in groups are named as people
 * ("In 2025, men spent 23 minutes more a day watching TV than women"); a
 * link's own labels only ever follow "for". When the API's interval
 * includes zero the sentence says so (review round 4): it reads the
 * interval's sign, makes no significance claim, and computes nothing.
 * `spec` (the request) tells built-in group names from a link's own.
 */
export function describeDifference(
  result: CompareResponse,
  spec?: CompareRequest,
): DifferenceDescription {
  const { difference, label_a: labelA, label_b: labelB } = result
  const unit = difference.unit
  const larger = difference.value > 0 ? 'a' : difference.value < 0 ? 'b' : null
  const populationOf = (group: CompareResponse['group_a']) =>
    (group.spec as { population?: PopulationRequest }).population
  const a = namedGroup(labelA, spec?.group_a, populationOf(result.group_a))
  const b = namedGroup(labelB, spec?.group_b, populationOf(result.group_b))
  const big = larger === 'b' ? b : a
  const small = larger === 'b' ? a : b

  const isTime = isDuration(unit)
  const isShare = unit === 'proportion_of_population'
  const word = isShare ? 'higher' : 'more'
  const magnitude = (value: number): string =>
    isTime
      ? formatDurationWords(value)
      : isShare
        ? formatPercentagePoints(Math.abs(value))
        : formatPeople(Math.abs(value))
  const amount =
    larger === null
      ? 'No difference'
      : `${magnitude(difference.value)} ${word}${isTime ? ' per day' : ''}`

  const { ci_lower: lower, ci_upper: upper, confidence_level: level } = difference
  const includesZero = lower != null && upper != null && lower <= 0 && upper >= 0
  let interval: string | null = null
  if (lower != null && upper != null && level != null) {
    // Whole minutes unless an end is under 10, where a decimal still shows
    // a narrow range's two ends apart (review round 4: "13.8 to 32.5 min").
    const precise = Math.min(Math.abs(lower), Math.abs(upper)) < 10
    const minutes = (value: number) =>
      precise
        ? formatMinutesDetailed(Math.abs(value))
        : formatCount(Math.round(Math.abs(value)))
    const bound = (value: number): string =>
      isTime
        ? `${minutes(value)} minutes`
        : isShare
          ? formatPercentagePoints(Math.abs(value))
          : formatPeople(Math.abs(value))
    const range = (from: number, to: number): string =>
      isTime
        ? `${minutes(from)} to ${bound(to)}`
        : isShare
          ? `${formatPercentagePoints(Math.abs(from), false)} to ${bound(to)}`
          : `${bound(from)} to ${bound(to)}`
    const prefix = `${confidenceIntervalName(level)}: `
    if (lower >= 0) {
      interval = `${prefix}${range(lower, upper)} ${word} for ${a.name}`
    } else if (upper <= 0) {
      interval = `${prefix}${range(upper, lower)} ${word} for ${b.name}`
    } else {
      interval = `${prefix}from ${bound(lower)} ${word} for ${b.name} to ${bound(upper)} ${word} for ${a.name}`
    }
  }

  const activity = activityPhrase(result.group_a.activity.label)
  const years = result.group_a.years
  const when =
    years.length === 1 ? `In ${formatYears(years)}` : `Across ${formatYears(years)} combined`
  const caveat =
    includesZero && larger !== null
      ? `, but the ${confidenceIntervalName(level)} for the difference includes zero`
      : ''

  let body: string | null = null
  if (larger !== null && big.who !== null && small.who !== null) {
    const value = magnitude(difference.value)
    if (big.who === small.who) {
      // The same people on different days: weekdays vs weekend days.
      const who = big.who
      switch (result.measure) {
        case 'average_minutes_per_day':
          body = `${who} spent ${value} more ${big.day.per} ${activity} than ${small.day.per}`
          break
        case 'participation_rate':
          body = `the share of ${who} who spent time ${activity} was ${value} higher on ${big.day.average} than on ${small.day.average}`
          break
        case 'average_minutes_per_participant':
          body = `${who} who spent any time ${activity} spent ${value} more on it on ${big.day.given} than on ${small.day.given}`
          break
        case 'participants_per_day':
          body = `an estimated ${value} more ${who} spent time ${activity} on ${big.day.average} than on ${small.day.average}`
          break
      }
    } else {
      switch (result.measure) {
        case 'average_minutes_per_day':
          body = `${big.who} spent ${value} more ${big.day.per} ${activity} than ${small.who}`
          break
        case 'participation_rate':
          body = `the share of ${big.who} who spent time ${activity} on ${big.day.average} was ${value} higher than the share of ${small.who}`
          break
        case 'average_minutes_per_participant':
          body = `${big.who} who spent any time ${activity} on ${big.day.given} spent ${value} more on it than ${small.who} who did`
          break
        case 'participants_per_day':
          body = `an estimated ${value} more ${big.who} than ${small.who} spent time ${activity} on ${big.day.average}`
          break
      }
    }
  }
  let sentence: string
  if (body !== null) {
    sentence = `${when}, ${body}${caveat}.`
  } else {
    const subject =
      measureSubject(result.measure, activity, a.day) ?? lowerFirst(measureName(result.measure))
    const comparative = isTime ? 'longer' : 'higher'
    sentence =
      larger === null
        ? `${when}, ${subject} was the same for ${a.name} and for ${b.name}.`
        : `${when}, ${subject} was ${magnitude(difference.value)} ${comparative} for ${big.name} than for ${small.name}${caveat}.`
  }
  return {
    larger,
    largerName: larger === null ? null : big.name,
    amount,
    interval,
    includesZero,
    sentence,
  }
}

/** "Average time…" → "average time…" (only the sentence's own first word; "TV…" stays). */
function lowerFirst(text: string): string {
  if (/^\p{Lu}{2,}\b/u.test(text)) return text
  return text.charAt(0).toLowerCase() + text.slice(1)
}

/** "men spent…" → "Men spent…". */
function upperFirst(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1)
}
