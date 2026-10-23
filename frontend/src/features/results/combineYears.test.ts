import { describe, expect, it } from 'vitest'

import type { AnalysisRequest } from '../../api/types'
import { decodeSpec } from '../../domain/urlSpec'
import { combineYearsLink } from './combineYears'

const YEARS = Array.from({ length: 23 }, (_, index) => 2003 + index)
const spec = (year: number, weights?: 'pandemic'): AnalysisRequest => ({
  measure: 'average_minutes_per_day',
  activity: { include: ['15'], label: 'Volunteer Activities' },
  years: [year],
  ...(weights ? { weights } : {}),
})
const yearsOf = (path: string) =>
  decodeSpec(new URL(path, 'http://x').searchParams.get('spec') ?? '', 'estimate')

describe('the latest years combined, for a figure from few diaries (review round 6, D7)', () => {
  it('combines the latest three years up to the figure’s own', () => {
    const link = combineYearsLink('estimate', spec(2025), YEARS)
    expect(link?.years).toBe('2023–2025')
    expect(yearsOf(link?.path ?? '').years).toEqual([2023, 2024, 2025])
  })

  it('leaves 2020 out of the standard weights’ years', () => {
    expect(combineYearsLink('estimate', spec(2021), YEARS)?.years).toBe('2018, 2019, 2021')
  })

  it('combines 2020 only with 2019, under the 2020 weights', () => {
    const link = combineYearsLink('estimate', spec(2020, 'pandemic'), YEARS)
    expect(link?.years).toBe('2019–2020')
    expect(yearsOf(link?.path ?? '').weights).toBe('pandemic')
  })

  it('offers nothing for a trend, a link already combined, or a first year alone', () => {
    expect(combineYearsLink('trend', spec(2025), YEARS)).toBeNull()
    expect(
      combineYearsLink('estimate', { ...spec(2025), years: [2024, 2025] }, YEARS),
    ).toBeNull()
    expect(combineYearsLink('estimate', spec(2003), YEARS)).toBeNull()
  })
})
