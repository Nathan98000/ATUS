import { describe, expect, it } from 'vitest'

import {
  estimatePandemicFixture,
  estimateSleepFixture,
  estimateTvFixture,
} from '../test/fixtures'
import {
  activityLabelFromSpec,
  analysisTitle,
  humanize,
  interpretEstimate,
  populationPhrase,
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
