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

describe('prototype-pollution key names (review finding)', () => {
  it('rejects specs containing __proto__/constructor/prototype keys', () => {
    for (const key of ['__proto__', 'constructor', 'prototype']) {
      const hostile = btoa(
        JSON.stringify({ activity: { preset: 'sleep' }, years: [2025], [key]: { x: 1 } }),
      )
      expect(() => decodeSpec(hostile, 'estimate')).toThrow(InvalidShareLinkError)
    }
  })
})

describe('deeply nested links (review round 1)', () => {
  /** base64url of a spec whose population holds `depth` nested arrays. */
  function nestedLink(depth: number): string {
    const arrays = `${'['.repeat(depth)}${']'.repeat(depth)}`
    const json = `{"activity":{"preset":"sleep"},"years":[2023],"population":{"x":${arrays}}}`
    return btoa(json).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '')
  }

  it('rejects a link nested deeper than any specification as an invalid link', () => {
    // ~8 KB, within the URL limit: JSON.parse accepts it, but the recursive
    // canonical serialization overflowed the stack and the app's error
    // boundary replaced the page instead of the invalid-link message.
    const encoded = nestedLink(3_000)
    expect(encoded.length).toBeLessThan(16_000)
    expect(() => decodeSpec(encoded, 'estimate')).toThrow(InvalidShareLinkError)
    expect(() => decodeSpec(nestedLink(11), 'estimate')).toThrow(/nested too deeply/)
  })

  it('accepts the deepest shape a specification has (spec → activity → include)', () => {
    const spec: CompareRequest = {
      activity: { include: ['010101'], exclude: ['010102'] },
      years: [2023],
      population: { age_min: 25 },
      group_a: { sex: 'male' },
      group_b: { sex: 'female' },
    }
    expect(decodeSpec(encodeSpec(spec), 'compare')).toEqual(spec)
    // A little nesting beyond that is left for the API to refuse (422).
    expect(() => decodeSpec(nestedLink(5), 'estimate')).not.toThrow()
  })
})
