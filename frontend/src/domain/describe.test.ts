import { describe, expect, it } from 'vitest'

import type { CompareRequest, CompareResponse, TrendResponse } from '../api/types'
import {
  compareChildrenFixture,
  estimatePandemicFixture,
  estimateSleepFixture,
  estimateTvFixture,
  trendLeisureFixture,
} from '../test/fixtures'
import {
  activityLabelFromSpec,
  activityName,
  analysisTitle,
  definitionNote,
  describeDifference,
  humanize,
  interpretSplitTrend,
  interpretEstimate,
  interpretTrend,
  offeredPresets,
  peoplePhrase,
  plainActivityName,
  populationPhrase,
  resultHeading,
  splitActivityLabel,
  stripVariableJargon,
  valueLabel,
} from './describe'

describe('populationPhrase', () => {
  it('describes an empty filter as the survey universe', () => {
    expect(populationPhrase({})).toBe('Everyone age 15 and over')
    expect(populationPhrase(undefined)).toBe('Everyone age 15 and over')
  })

  it('names an open-ended age band in words, like the others (review round 4)', () => {
    expect(populationPhrase({ age_min: 65 })).toBe('Ages 65 and over')
  })
})

describe('peoplePhrase (review round 4)', () => {
  it('names the population as the people a sentence talks about', () => {
    expect(peoplePhrase({})).toBe('Americans age 15 and over')
    expect(peoplePhrase({ day_type: 'weekend' })).toBe('Americans age 15 and over')
    expect(peoplePhrase({ sex: 'female', age_min: 25, age_max: 54 })).toBe('women ages 25–54')
    expect(peoplePhrase({ age_min: 65 })).toBe('people ages 65 and over')
    expect(peoplePhrase({ sex: 'male', employment_status: 'employed' })).toBe('employed men')
    expect(peoplePhrase({ employment_status: 'not_in_labor_force' })).toBe(
      'people not in the labor force',
    )
    expect(
      peoplePhrase({ has_household_children: true, education_level: 'bachelor_or_higher' }),
    ).toBe("people with a bachelor's degree or higher and with children under 18 at home")
    expect(peoplePhrase({ has_household_children: false })).toBe(
      'people without children under 18 at home',
    )
    expect(peoplePhrase({ region: 3 })).toBe('people in the South')
  })
})

describe('plainActivityName (review round 4)', () => {
  it('spells out the survey’s abbreviations and Title Case', () => {
    expect(plainActivityName('Caring For & Helping HH Children')).toBe(
      'Caring for and helping household children',
    )
    expect(plainActivityName("Activities Related to Nonhh Children's Education")).toBe(
      "Activities related to non-household children's education",
    )
    expect(plainActivityName('HH & personal mail & messages (except e-mail)')).toBe(
      'Household and personal mail and messages (except e-mail)',
    )
    expect(plainActivityName('Shopping (Store, Telephone, Internet)')).toBe(
      'Shopping (store, telephone, internet)',
    )
    expect(plainActivityName('Work & Work-Related Activities')).toBe(
      'Work and work-related activities',
    )
  })

  it('gives the hardest category names plain ones, and leaves plain names alone', () => {
    expect(plainActivityName('Volunteer Activities')).toBe('Volunteering')
    expect(plainActivityName('Caring For & Helping Household (HH) Members')).toBe(
      'Caring for household members',
    )
    expect(plainActivityName('Watching TV')).toBe('Watching TV')
    expect(plainActivityName('Working (main and other jobs)')).toBe(
      'Working (main and other jobs)',
    )
    expect(activityName('Leisure and sports (BLS table A-1 definition)')).toBe(
      'Leisure and sports',
    )
  })

  it('combines dimensions in a readable order', () => {
    expect(populationPhrase({ sex: 'female', age_min: 25, age_max: 54 })).toBe(
      'Women · ages 25–54',
    )
    expect(populationPhrase({ employment_status: 'not_in_labor_force' })).toBe(
      'Not in the labor force',
    )
    expect(populationPhrase({ has_household_children: false })).toBe(
      'Without household children',
    )
  })
})

describe('titles', () => {
  it('derives a title from activity, population and years', () => {
    expect(analysisTitle('estimate', estimateSleepFixture.spec as never, 'Sleeping')).toBe(
      'Sleeping — Ages 25–54 — 2025',
    )
  })

  it('uses group labels for comparisons', () => {
    const spec = {
      activity: { preset: 'sleep' },
      years: [2025],
      group_a: {},
      group_b: {},
      label_a: 'Men',
      label_b: 'Women',
    }
    expect(analysisTitle('compare', spec as never, 'Sleeping')).toBe(
      'Sleeping — Men vs Women — 2025',
    )
  })

  it("names a comparison's shared population, which restricts both groups (review round 3)", () => {
    const spec = {
      activity: { preset: 'sleep' },
      years: [2025],
      population: { age_min: 65 },
      group_a: { sex: 'male' },
      group_b: { sex: 'female' },
      label_a: 'Men',
      label_b: 'Women',
    }
    expect(analysisTitle('compare', spec as never, 'Sleeping')).toBe(
      'Sleeping — Men vs Women — Ages 65 and over — 2025',
    )
    // Unset fields restrict nothing.
    const unset = { ...spec, population: { age_min: null } }
    expect(analysisTitle('compare', unset as never, 'Sleeping')).toBe(
      'Sleeping — Men vs Women — 2025',
    )
  })

  it('labels activities from spec form before results arrive', () => {
    expect(activityLabelFromSpec({ activity: { preset: 'sleep' }, years: [2025] })).toBe(
      'Sleep',
    )
    expect(
      activityLabelFromSpec({
        activity: { include: ['1203'], label: 'Relaxing' },
        years: [2025],
      }),
    ).toBe('Relaxing')
  })
})

describe('interpretEstimate', () => {
  it('describes an average without causal language', () => {
    const sentence = interpretEstimate(estimateSleepFixture)
    expect(sentence).toBe(
      'People ages 25–54 spent an estimated 8 hours 49 minutes a day sleeping in 2025, ' +
        'averaged across all days of the week.',
    )
    expect(sentence).not.toMatch(/because|causes|due to/)
  })

  it('reads as a sentence: no quotation marks, nested brackets or interpuncts (review round 4)', () => {
    for (const fixture of [estimateSleepFixture, estimateTvFixture, estimatePandemicFixture]) {
      expect(interpretEstimate(fixture)).not.toMatch(/[“”()·]/)
    }
  })

  it('describes participation as a daily rate', () => {
    const sentence = interpretEstimate(estimateTvFixture)
    expect(sentence).toContain('72.8%')
    expect(sentence).toContain('average day')
  })

  it('handles the pandemic estimate deterministically', () => {
    expect(interpretEstimate(estimatePandemicFixture)).toContain('2020')
  })
})

describe('labels and jargon', () => {
  it('maps known values to human labels and humanizes unknown ones', () => {
    expect(valueLabel('employment_status', 'not_in_labor_force')).toBe('Not in the labor force')
    expect(valueLabel('region', 3)).toBe('South')
    expect(valueLabel('sex', 'female')).toBe('Female')
    expect(valueLabel('education_level', 'future_new_level')).toBe('Future new level')
    expect(humanize('participants_per_day')).toBe('Participants per day')
  })

  it('strips internal variable references from API descriptions', () => {
    expect(
      stripVariableJargon(
        'Any household child under 18 present (TRCHILDNUM > 0). Household children, not necessarily own children.',
      ),
    ).toBe(
      'Any household child under 18 present. Household children, not necessarily own children.',
    )
    expect(stripVariableJargon('Respondent sex from the ATUS household roster (TESEX).')).toBe(
      'Respondent sex from the ATUS household roster.',
    )
  })
})

describe('interpretation faithfulness (review findings)', () => {
  it('never claims "all days of the week" for day-type-filtered estimates', () => {
    const weekend = {
      ...estimateSleepFixture,
      spec: {
        ...(estimateSleepFixture.spec as Record<string, unknown>),
        population: { day_type: 'weekend' },
      },
    }
    const sentence = interpretEstimate(weekend)
    expect(sentence).toContain('per weekend day')
    expect(sentence).not.toContain('all days of the week')
  })

  it('mentions the diary window when date filters are set', () => {
    const windowed = {
      ...estimateSleepFixture,
      spec: {
        ...(estimateSleepFixture.spec as Record<string, unknown>),
        population: { diary_date_min: '2020-05-10' },
      },
    }
    expect(interpretEstimate(windowed)).toContain('within the selected diary period')
  })

  it('never uses a non-person filter as the sentence subject', () => {
    const fips = {
      ...estimateSleepFixture,
      spec: {
        ...(estimateSleepFixture.spec as Record<string, unknown>),
        population: { state_fips: '06' },
      },
    }
    const sentence = interpretEstimate(fips)
    expect(sentence).toMatch(/^People in state FIPS 06 spent/)
  })
})

describe('splitActivityLabel', () => {
  it('moves a BLS-table definition out of the name', () => {
    expect(splitActivityLabel('Leisure and sports (BLS table A-1 definition)')).toEqual({
      name: 'Leisure and sports',
      qualifier: 'As defined in BLS table A-1',
    })
  })

  it('leaves every other label alone', () => {
    expect(splitActivityLabel('Working (main and other jobs)')).toEqual({
      name: 'Working (main and other jobs)',
      qualifier: null,
    })
  })
})

describe('resultHeading', () => {
  it('says the statistic, the activity, and the rest once', () => {
    expect(resultHeading('estimate', estimateSleepFixture.spec as never, 'Sleeping')).toEqual({
      eyebrow: 'Average time per day',
      title: 'Sleeping',
      subtitle: 'Ages 25–54 · 2025',
    })
  })

  it('drops a definition qualifier and names a trend by year', () => {
    const heading = resultHeading(
      'trend',
      trendLeisureFixture.spec as never,
      trendLeisureFixture.activity.label,
    )
    expect(heading.eyebrow).toBe('Average time per day, by year')
    expect(heading.title).toBe('Leisure and sports')
    expect(heading.subtitle).toBe('Everyone age 15 and over · 2003–2025')
  })

  it('offers one preset per name, the BLS-table one where two share it', () => {
    const presets = [
      { name: 'eating_and_drinking', label: 'Eating and drinking' },
      { name: 'eating_bls', label: 'Eating and drinking (BLS table A-1 definition)' },
      { name: 'sleep', label: 'Sleeping' },
    ]
    expect(offeredPresets(presets).map((preset) => preset.name)).toEqual([
      'eating_bls',
      'sleep',
    ])
    expect(activityName(presets[1]?.label as string)).toBe('Eating and drinking')
  })

  it("names a comparison's groups in the title and its shared filters below", () => {
    const spec = {
      activity: { preset: 'sleep' },
      years: [2025],
      population: { age_min: 65 },
      group_a: { sex: 'male' },
      group_b: { sex: 'female' },
      label_a: 'Men',
      label_b: 'Women',
    }
    expect(resultHeading('compare', spec as never, 'Sleeping')).toMatchObject({
      title: 'Sleeping: Men vs Women',
      subtitle: 'Both groups: Ages 65 and over · 2025',
    })
  })
})

describe('interpretTrend', () => {
  it('leads with the latest and first year and the range between', () => {
    expect(interpretTrend(trendLeisureFixture)).toBe(
      'Americans age 15 and over spent 5 hours 10 minutes a day on leisure and sports in ' +
        '2025, compared with 5 hours 7 minutes in 2003. Over these years it ranged from ' +
        '5 hours 4 minutes in 2024 to 5 hours 22 minutes in 2012.',
    )
  })

  it('names no range when the chart draws a partial year (review round 5)', () => {
    expect(interpretTrend(trendLeisureFixture, [2020])).toBe(
      'Americans age 15 and over spent 5 hours 10 minutes a day on leisure and sports in ' +
        '2025, compared with 5 hours 7 minutes in 2003.',
    )
  })

  it('names a filtered population and skips the range for two years', () => {
    const twoYears: TrendResponse = {
      ...trendLeisureFixture,
      spec: { ...trendLeisureFixture.spec, population: { sex: 'female' } },
      points: trendLeisureFixture.points.filter((point) => [2019, 2021].includes(point.year)),
    }
    expect(interpretTrend(twoYears)).toBe(
      'Women spent 5 hours 16 minutes a day on leisure and sports in 2021, compared with 5 hours 11 minutes in 2019.',
    )
  })

  it('says nothing when no year has an estimate', () => {
    const none: TrendResponse = {
      ...trendLeisureFixture,
      points: trendLeisureFixture.points.filter((point) => point.estimate == null),
    }
    expect(interpretTrend(none)).toBe('')
  })
})

describe('describeDifference', () => {
  it('names the larger group and the interval, in words', () => {
    // The fixture's difference is negative: B ("No household children") is larger.
    // The fixture's labels are its own, not the plain descriptions of the
    // groups: they only ever follow "for".
    expect(describeDifference(compareChildrenFixture)).toEqual({
      larger: 'b',
      largerName: 'No household children',
      amount: '11 minutes more per day',
      interval: '95% confidence interval: 2.8 to 19.4 minutes more for No household children',
      includesZero: false,
      sentence:
        'In 2025, average time a day spent on household activities was 11 minutes longer for ' +
        'No household children than for Household children present.',
    })
  })

  it('names built-in groups as people (review round 4)', () => {
    const spec = {
      activity: { preset: 'household_activities_bls_table' },
      years: [2025],
      group_a: { has_household_children: true },
      group_b: { has_household_children: false },
      label_a: 'With household children',
      label_b: 'Without household children',
    } as CompareRequest
    const builtIn: CompareResponse = {
      ...compareChildrenFixture,
      label_a: 'With household children',
      label_b: 'Without household children',
    }
    const described = describeDifference(builtIn, spec)
    expect(described.sentence).toBe(
      'In 2025, people without children under 18 at home spent 11 minutes more a day on ' +
        'household activities than people with children under 18 at home.',
    )
    expect(described.largerName).toBe('people without children under 18 at home')
    expect(described.interval).toBe(
      '95% confidence interval: 2.8 to 19.4 minutes more for people without children under 18 at home',
    )
  })

  it('says when the confidence interval includes zero (review round 4)', () => {
    const straddling: CompareResponse = {
      ...compareChildrenFixture,
      difference: {
        ...compareChildrenFixture.difference,
        value: 0.4,
        ci_lower: -4,
        ci_upper: 4.3,
      },
    }
    const described = describeDifference(straddling)
    expect(described.includesZero).toBe(true)
    expect(described.sentence).toMatch(
      /, but the 95% confidence interval for the difference includes zero\.$/,
    )
  })

  it('spans both groups when the interval includes zero', () => {
    const straddling: CompareResponse = {
      ...compareChildrenFixture,
      difference: {
        ...compareChildrenFixture.difference,
        value: 2.4,
        ci_lower: -3.1,
        ci_upper: 7.9,
      },
    }
    const described = describeDifference(straddling)
    expect(described.larger).toBe('a')
    expect(described.interval).toBe(
      '95% confidence interval: from 3.1 minutes more for No household children to 7.9 minutes more for Household children present',
    )
  })

  it('says when there is no difference at all, and pools years in words', () => {
    const equal: CompareResponse = {
      ...compareChildrenFixture,
      group_a: { ...compareChildrenFixture.group_a, years: [2023, 2024, 2025] },
      difference: { ...compareChildrenFixture.difference, value: 0 },
    }
    const described = describeDifference(equal)
    expect(described.larger).toBeNull()
    expect(described.amount).toBe('No difference')
    expect(described.sentence).toBe(
      'Across 2023–2025 combined, average time a day spent on household activities was the ' +
        'same for Household children present and for No household children.',
    )
  })
})

describe('definitionNote (R14)', () => {
  const presets = [{ label: 'Sleeping', include: ['0101'], exclude: [] }]
  const surveyNames = new Map([
    ['0101', 'Sleeping'],
    ['15', 'Volunteer Activities'],
    ['120303', 'Television and movies (not religious)'],
  ])

  it('says nothing when the names are the plain descriptions and the activity a preset', () => {
    expect(
      definitionNote('compare', {
        activity: { preset: 'sleep' },
        years: [2025],
        group_a: { sex: 'male' },
        group_b: { sex: 'female' },
        label_a: 'Men',
        label_b: 'Women',
      }),
    ).toBeNull()
    // A preset's canonical link: its codes under its own name.
    expect(
      definitionNote(
        'trend',
        { activity: { include: ['0101'], exclude: [], label: 'Sleeping' }, years: [2025] },
        presets,
      ),
    ).toBeNull()
  })

  it('sets names a link chose against the filters computed', () => {
    expect(
      definitionNote('compare', {
        activity: { preset: 'sleep' },
        years: [2025],
        group_a: { sex: 'male' },
        group_b: { sex: 'female' },
        label_a: 'Women',
        label_b: 'Men',
      }),
    ).toBe('Groups compared: Women (men) and Men (women).')
  })

  it('says nothing about an activity under the survey’s own name (review round 5)', () => {
    // What the builder writes for a category or an activity it lists.
    for (const [code, name] of surveyNames) {
      expect(
        definitionNote(
          'estimate',
          { activity: { include: [code], label: name }, years: [2025] },
          presets,
          surveyNames,
        ),
      ).toBeNull()
    }
    // No label: the heading names the codes, not a chosen name.
    expect(
      definitionNote(
        'estimate',
        { activity: { include: ['120303'] }, years: [2025] },
        presets,
        surveyNames,
      ),
    ).toBeNull()
  })

  it('names the codes behind any other activity name', () => {
    expect(
      definitionNote(
        'estimate',
        { activity: { include: ['0101'], label: 'Working' }, years: [2025] },
        presets,
        surveyNames,
      ),
    ).toBe('“Working” here means survey activity code 0101 and the activities under it.')
    expect(
      definitionNote(
        'estimate',
        {
          activity: { include: ['02', '1802'], exclude: ['020903'], label: 'Chores' },
          years: [2025],
        },
        presets,
        surveyNames,
      ),
    ).toBe(
      '“Chores” here means survey activity codes 02, 1802 and the activities under them, except 020903.',
    )
    // A survey name on codes it does not belong to is still a chosen name.
    expect(
      definitionNote(
        'estimate',
        { activity: { include: ['15'], label: 'Sleeping' }, years: [2025] },
        presets,
        surveyNames,
      ),
    ).toBe('“Sleeping” here means survey activity code 15 and the activities under it.')
  })

  it('says nothing about the activity until the survey’s names are known', () => {
    expect(
      definitionNote('estimate', {
        activity: { include: ['0101'], label: 'Working' },
        years: [2025],
      }),
    ).toBeNull()
  })
})

describe('interpretSplitTrend', () => {
  const shifted = (minutes: number): TrendResponse => ({
    ...trendLeisureFixture,
    points: trendLeisureFixture.points.map((point) =>
      point.estimate == null
        ? point
        : { ...point, estimate: { ...point.estimate, value: point.estimate.value + minutes } },
    ),
  })

  const forGroup = (result: TrendResponse, population: Record<string, unknown>) => ({
    ...result,
    spec: { ...result.spec, population },
  })

  it('names each group’s latest value when there are two or three', () => {
    // 2025: 309.6 minutes; 60 less is 249.6.
    expect(
      interpretSplitTrend([
        {
          label: 'Men',
          filters: { sex: 'male' },
          result: forGroup(trendLeisureFixture, { sex: 'male' }),
        },
        {
          label: 'Women',
          filters: { sex: 'female' },
          result: forGroup(shifted(-60), { sex: 'female' }),
        },
      ]),
    ).toBe(
      'In 2025, men spent 5 hours 10 minutes a day on leisure and sports, and women 4 hours 10 minutes.',
    )
  })

  it('names weekdays and weekend days as days', () => {
    expect(
      interpretSplitTrend([
        {
          label: 'Weekdays',
          filters: { day_type: 'weekday' },
          result: forGroup(trendLeisureFixture, { day_type: 'weekday' }),
        },
        {
          label: 'Weekend days',
          filters: { day_type: 'weekend' },
          result: forGroup(shifted(60), { day_type: 'weekend' }),
        },
      ]),
    ).toBe(
      'In 2025, Americans age 15 and over spent 5 hours 10 minutes per weekday on leisure and sports, and 6 hours 10 minutes per weekend day.',
    )
  })

  it('keeps a link’s own group names after "for"', () => {
    expect(
      interpretSplitTrend([
        { label: 'Men', result: trendLeisureFixture },
        { label: 'Women', result: shifted(-60) },
      ]),
    ).toBe(
      'In 2025, average time a day spent on leisure and sports was 5 hours 10 minutes for Men and 4 hours 10 minutes for Women.',
    )
  })

  it('names the highest and the lowest when there are more', () => {
    const series = [0, 30, -30, 10].map((minutes, index) => ({
      label: `Group ${index + 1}`,
      result: shifted(minutes),
    }))
    expect(interpretSplitTrend(series)).toBe(
      'In 2025, average time a day spent on leisure and sports was highest for Group 2 (5 hours 40 minutes) and lowest for Group 3 (4 hours 40 minutes).',
    )
  })
})
