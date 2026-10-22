import { describe, expect, it } from 'vitest'

import type { CompareResponse, TrendResponse } from '../api/types'
import {
  compareChildrenFixture,
  estimatePandemicFixture,
  estimateSleepFixture,
  estimateTvFixture,
  trendLeisureFixture,
} from '../test/fixtures'
import {
  activityLabelFromSpec,
  analysisTitle,
  definitionNote,
  describeDifference,
  humanize,
  interpretSplitTrend,
  interpretEstimate,
  interpretTrend,
  populationPhrase,
  resultHeading,
  splitActivityLabel,
  stripVariableJargon,
  valueLabel,
} from './describe'

describe('populationPhrase', () => {
  it('describes an empty filter as the survey universe', () => {
    expect(populationPhrase({})).toBe('All respondents (age 15+)')
    expect(populationPhrase(undefined)).toBe('All respondents (age 15+)')
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
      'Sleeping — Men vs Women — Age 65+ — 2025',
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
    expect(sentence).toContain('8h 49m')
    expect(sentence).toContain('Sleeping')
    expect(sentence).toContain('2025')
    expect(sentence).not.toMatch(/because|causes|due to/)
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
    expect(sentence).toContain('averaged across weekend days')
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
    expect(sentence).toMatch(/^People in the selected population spent/)
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

  it('keeps a definition qualifier and names a trend by year', () => {
    const heading = resultHeading(
      'trend',
      trendLeisureFixture.spec as never,
      trendLeisureFixture.activity.label,
    )
    expect(heading.eyebrow).toBe('Average time per day, by year')
    expect(heading.title).toBe('Leisure and sports')
    expect(heading.subtitle).toBe(
      'As defined in BLS table A-1 · All respondents (age 15+) · 2003–2025',
    )
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
      subtitle: 'Both groups: Age 65+ · 2025',
    })
  })
})

describe('interpretTrend', () => {
  it('leads with the latest and first year and the range between', () => {
    expect(interpretTrend(trendLeisureFixture)).toBe(
      'Average time per day on “Leisure and sports” was 5h 10m in 2025, compared with ' +
        '5h 7m in 2003. Over these years it ranged from 5h 4m in 2024 to 5h 22m in 2012.',
    )
  })

  it('names a filtered population and skips the range for two years', () => {
    const twoYears: TrendResponse = {
      ...trendLeisureFixture,
      spec: { ...trendLeisureFixture.spec, population: { sex: 'female' } },
      points: trendLeisureFixture.points.filter((point) => [2019, 2021].includes(point.year)),
    }
    expect(interpretTrend(twoYears)).toBe(
      'Average time per day on “Leisure and sports” (Women) was 5h 16m in 2021, compared with 5h 11m in 2019.',
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
    expect(describeDifference(compareChildrenFixture)).toEqual({
      larger: 'b',
      amount: '11 minutes more per day',
      interval: 'Likely range: 2.8 to 19.4 min more for No household children (95% confidence)',
      sentence:
        'In 2025, average time per day on “Household activities” was 11 minutes longer for ' +
        'No household children than for Household children present.',
    })
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
      'Likely range: from 3.1 min more for No household children to 7.9 min more for Household children present (95% confidence)',
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
      'Across 2023–2025 combined, average time per day on “Household activities” was the same ' +
        'for Household children present and for No household children.',
    )
  })
})

describe('definitionNote (R14)', () => {
  const presets = [{ label: 'Sleeping', include: ['0101'], exclude: [] }]

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

  it('names the codes behind a custom activity name', () => {
    expect(
      definitionNote(
        'estimate',
        { activity: { include: ['0101'], label: 'Working' }, years: [2025] },
        presets,
      ),
    ).toBe('Activity code 0101.')
    expect(
      definitionNote('estimate', {
        activity: { include: ['02', '1802'], exclude: ['020903'], label: 'Chores' },
        years: [2025],
      }),
    ).toBe('Activity codes 02, 1802, excluding 020903.')
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

  it('names each group’s latest value when there are two or three', () => {
    // 2025: 309.6 minutes; 60 less is 249.6.
    expect(
      interpretSplitTrend([
        { label: 'Men', result: trendLeisureFixture },
        { label: 'Women', result: shifted(-60) },
      ]),
    ).toBe(
      'In 2025, average time per day on “Leisure and sports” was 5h 10m for Men and 4h 10m for Women.',
    )
  })

  it('names the highest and the lowest when there are more', () => {
    const series = [0, 30, -30, 10].map((minutes, index) => ({
      label: `Group ${index + 1}`,
      result: shifted(minutes),
    }))
    expect(interpretSplitTrend(series)).toBe(
      'In 2025, average time per day on “Leisure and sports” was highest for Group 2 (5h 40m) and lowest for Group 3 (4h 40m).',
    )
  })
})
