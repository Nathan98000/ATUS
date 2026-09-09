import { describe, expect, it } from 'vitest'

import type { CompareRequest, EstimateRequest } from '../api/types'
import { InvalidShareLinkError, analysisPath, decodeSpec, encodeSpec } from './urlSpec'

const estimate: EstimateRequest = {
  measure: 'average_minutes_per_day',
  activity: { preset: 'sleep' },
  years: [2025],
  population: { age_min: 25, age_max: 54 },
}

describe('encodeSpec / decodeSpec', () => {
  it('round-trips a spec losslessly', () => {
    expect(decodeSpec(encodeSpec(estimate), 'estimate')).toEqual(estimate)
  })

  it('round-trips non-ASCII labels (URL-safe base64 of UTF-8)', () => {
    const spec: EstimateRequest = {
      activity: { include: ['120303'], label: 'Fernsehen — „Filme“ & TV 📺' },
      years: [2024],
    }
    const encoded = encodeSpec(spec)
    expect(encoded).toMatch(/^[A-Za-z0-9_-]+$/) // no characters needing URL escaping
    expect(decodeSpec(encoded, 'estimate')).toEqual(spec)
  })

  it('produces identical URLs for specs that differ only in key order', () => {
    const reordered = {
      years: [2025],
      population: { age_max: 54, age_min: 25 },
      activity: { preset: 'sleep' },
      measure: 'average_minutes_per_day',
    } as EstimateRequest
    expect(encodeSpec(reordered)).toBe(encodeSpec(estimate))
  })

  it('builds the /analysis path', () => {
    expect(analysisPath('estimate', estimate)).toMatch(
      /^\/analysis\/estimate\?spec=[A-Za-z0-9_-]+$/,
    )
  })
})

describe('invalid share links', () => {
  it('rejects garbage encodings', () => {
    expect(() => decodeSpec('!!!not-base64!!!', 'estimate')).toThrow(InvalidShareLinkError)
  })

  it('rejects valid base64 that is not JSON', () => {
    expect(() => decodeSpec(btoa('just some text'), 'estimate')).toThrow(InvalidShareLinkError)
  })

  it('rejects specs without an activity or years', () => {
    const noActivity = btoa(JSON.stringify({ years: [2025] }))
    const noYears = btoa(JSON.stringify({ activity: { preset: 'sleep' } }))
    expect(() => decodeSpec(noActivity, 'estimate')).toThrow(/activity/)
    expect(() => decodeSpec(noYears, 'estimate')).toThrow(/years/)
  })

  it('requires both groups for compare links', () => {
    const missingGroup = btoa(
      JSON.stringify({ activity: { preset: 'sleep' }, years: [2025], group_a: {} }),
    )
    expect(() => decodeSpec(missingGroup, 'compare')).toThrow(/both groups/)
  })

  it('accepts a structurally plausible compare spec', () => {
    const compare: CompareRequest = {
      activity: { preset: 'sleep' },
      years: [2025],
      group_a: { sex: 'male' },
      group_b: { sex: 'female' },
    }
    expect(decodeSpec(encodeSpec(compare), 'compare')).toEqual(compare)
  })
})
