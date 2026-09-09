import { describe, expect, it } from 'vitest'

import type { CompareRequest } from '../../api/types'
import {
  defaultBuilderState,
  fromRequest,
  prunePopulation,
  selectedYears,
  toRequest,
  validateBuilder,
} from './state'

const YEARS = Array.from({ length: 23 }, (_, index) => 2003 + index) // 2003–2025

describe('toRequest', () => {
  it('builds a single-year estimate request with explicit methodology', () => {
    const state = defaultBuilderState(YEARS)
    expect(toRequest(state, YEARS)).toEqual({
      measure: 'average_minutes_per_day',
      activity: { preset: 'sleep' },
      years: [2025],
      population: {},
      weights: 'multiyear',
      variance: 'replicate',
      confidence_level: 0.95,
    })
  })

  it('sorts pooled years and prunes unset population fields', () => {
    const state = defaultBuilderState(YEARS)
    state.yearsMode = 'pooled'
    state.pooledYears = [2024, 2022, 2023]
    state.population = { sex: 'female', age_min: undefined }
    const request = toRequest(state, YEARS)
    expect(request.years).toEqual([2022, 2023, 2024])
    expect(request.population).toEqual({ sex: 'female' })
  })

  it('expands a trend range to the years the API actually has', () => {
    const state = defaultBuilderState(YEARS)
    state.operation = 'trend'
    state.trendFrom = 2019
    state.trendTo = 2022
    expect(toRequest(state, YEARS).years).toEqual([2019, 2020, 2021, 2022])
    // A year missing from the data release is never requested.
    expect(toRequest(state, [2019, 2021, 2022]).years).toEqual([2019, 2021, 2022])
  })

  it('includes groups and labels for comparisons', () => {
    const state = defaultBuilderState(YEARS)
    state.operation = 'compare'
    state.groupA = { sex: 'male' }
    state.groupB = { sex: 'female' }
    state.labelA = 'Men'
    state.labelB = 'Women'
    const request = toRequest(state, YEARS) as CompareRequest
    expect(request.group_a).toEqual({ sex: 'male' })
    expect(request.label_b).toBe('Women')
  })
})

describe('fromRequest (deep links back into the builder)', () => {
  it('round-trips an estimate spec', () => {
    const original = toRequest(defaultBuilderState(YEARS), YEARS)
    const rebuilt = fromRequest('estimate', original, YEARS)
    expect(toRequest(rebuilt, YEARS)).toEqual(original)
  })

  it('round-trips a compare spec including groups', () => {
    const state = defaultBuilderState(YEARS)
    state.operation = 'compare'
    state.groupA = { has_household_children: true }
    state.groupB = { has_household_children: false }
    state.labelA = 'With children'
    state.labelB = 'Without children'
    const original = toRequest(state, YEARS)
    const rebuilt = fromRequest('compare', original, YEARS)
    expect(toRequest(rebuilt, YEARS)).toEqual(original)
  })

  it('maps multi-year specs to pooled mode and trend specs to a range', () => {
    const pooled = fromRequest(
      'estimate',
      { activity: { preset: 'sleep' }, years: [2023, 2025] },
      YEARS,
    )
    expect(pooled.yearsMode).toBe('pooled')
    expect(pooled.pooledYears).toEqual([2023, 2025])

    const trend = fromRequest(
      'trend',
      { activity: { preset: 'sleep' }, years: [2019, 2020, 2021] },
      YEARS,
    )
    expect(trend.trendFrom).toBe(2019)
    expect(trend.trendTo).toBe(2021)
  })

  it('keeps unknown future measures out of the builder state', () => {
    const rebuilt = fromRequest(
      'estimate',
      { activity: { preset: 'sleep' }, years: [2025], measure: 'median_minutes' as never },
      YEARS,
    )
    expect(rebuilt.measure).toBe('average_minutes_per_day')
  })
})

describe('validateBuilder', () => {
  it('accepts the default analysis', () => {
    expect(validateBuilder(defaultBuilderState(YEARS), YEARS).valid).toBe(true)
  })

  it('rejects age minimum above maximum', () => {
    const state = defaultBuilderState(YEARS)
    state.population = { age_min: 90, age_max: 20 }
    const validation = validateBuilder(state, YEARS)
    expect(validation.valid).toBe(false)
    expect(validation.errors.age).toMatch(/Minimum age/)
  })

  it('rejects an empty pooled selection and an inverted trend range', () => {
    const pooled = defaultBuilderState(YEARS)
    pooled.yearsMode = 'pooled'
    pooled.pooledYears = []
    expect(validateBuilder(pooled, YEARS).errors.years).toBeTruthy()

    const trend = defaultBuilderState(YEARS)
    trend.operation = 'trend'
    trend.trendFrom = 2024
    trend.trendTo = 2019
    expect(validateBuilder(trend, YEARS).errors.years).toMatch(/start year/)
  })

  it('validates group ages independently in compare mode', () => {
    const state = defaultBuilderState(YEARS)
    state.operation = 'compare'
    state.groupB = { age_min: 60, age_max: 30 }
    expect(validateBuilder(state, YEARS).errors.ageB).toBeTruthy()
  })
})

describe('prunePopulation / selectedYears', () => {
  it('drops only null and undefined values', () => {
    expect(
      prunePopulation({ sex: 'male', age_min: undefined, has_household_children: false }),
    ).toEqual({ sex: 'male', has_household_children: false })
  })

  it('returns an empty list when nothing is selected', () => {
    const state = defaultBuilderState([])
    expect(selectedYears(state, YEARS)).toEqual([])
  })
})
