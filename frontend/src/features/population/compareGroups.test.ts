import { describe, expect, it } from 'vitest'

import { populationMetadataFixture } from '../../test/fixtures'
import {
  compareChoices,
  defaultPair,
  deriveCompareBy,
  resolveSplit,
  seriesSpec,
} from './compareGroups'

const dimensions = populationMetadataFixture.dimensions

describe('compare-by choices', () => {
  it('offers only what the population metadata reports, each value plainly named', () => {
    const choices = compareChoices(dimensions)
    expect(choices.map((choice) => choice.by)).toEqual([
      'sex',
      'age',
      'employment_status',
      'education_level',
      'has_household_children',
      'day_type',
    ])
    const sex = choices[0]
    expect(sex?.options.map((option) => option.label)).toEqual(['Men', 'Women'])
    const age = choices.find((choice) => choice.by === 'age')
    expect(age?.options.at(-1)).toEqual({
      key: '65+',
      label: 'Age 65+',
      population: { age_min: 65 },
    })
  })

  it('starts each comparison from a contrast worth looking at', () => {
    const education = compareChoices(dimensions).find((c) => c.by === 'education_level')
    const [a, b] = defaultPair(education as NonNullable<typeof education>)
    expect([a.key, b.key]).toEqual(['bachelor_or_higher', 'high_school'])
  })

  it('recognizes a simple comparison only under its plain names', () => {
    const choices = compareChoices(dimensions)
    const groups = { groupA: { sex: 'male' }, groupB: { sex: 'female' } } as const
    expect(deriveCompareBy(choices, { ...groups, labelA: 'Men', labelB: 'Women' })).toBe('sex')
    expect(deriveCompareBy(choices, { ...groups, labelA: 'Dads', labelB: 'Moms' })).toBe(
      'custom',
    )
    expect(
      deriveCompareBy(choices, {
        groupA: { sex: 'male', day_type: 'weekend' },
        groupB: { sex: 'female' },
        labelA: 'Men',
        labelB: 'Women',
      }),
    ).toBe('custom')
  })
})

describe('split trends', () => {
  it('resolves a split against this release, in the choice’s own order', () => {
    const resolved = resolveSplit(dimensions, { by: 'sex', groups: ['female', 'male'] })
    expect(resolved?.options.map((option) => option.label)).toEqual(['Men', 'Women'])
    expect(resolveSplit(dimensions, { by: 'sex', groups: ['male', 'other'] })).toBeNull()
    expect(resolveSplit(dimensions, { by: 'region', groups: ['1', '2'] })).toBeNull()
  })

  it('asks for each group’s line as point estimates, its filter in place of the trend’s', () => {
    const resolved = resolveSplit(dimensions, { by: 'age', groups: ['15-24', '65+'] })
    const spec = {
      activity: { preset: 'sleep' },
      years: [2024, 2025],
      population: { age_max: 40, sex: 'female' as const },
      variance: 'replicate' as const,
    }
    const [young, old] = (resolved?.options ?? []).map((option) =>
      seriesSpec(spec, resolved as NonNullable<typeof resolved>, option),
    )
    expect(young).toEqual({
      ...spec,
      population: { sex: 'female', age_min: 15, age_max: 24 },
      variance: 'none',
    })
    expect(old?.population).toEqual({ sex: 'female', age_min: 65 })
  })
})
