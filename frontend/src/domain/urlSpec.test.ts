import { describe, expect, it } from 'vitest'

import type { CompareRequest, EstimateRequest, Operation } from '../api/types'
import {
  compareChildrenFixture,
  estimatePandemicFixture,
  estimateSleepFixture,
  metaFixture,
  trendLeisureFixture,
} from '../test/fixtures'
import { STARTER_EXAMPLES } from './prewarm'
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
    // A little nesting beyond that is left for the API to refuse (422). (In a
    // field the page reads, such as a population value, a list is now the
    // wrong type: review round 2.)
    const nestedField = `{"activity":{"preset":"sleep"},"extra":${'['.repeat(5)}${']'.repeat(5)},"years":[2023]}`
    const encoded = btoa(nestedField)
      .replaceAll('+', '-')
      .replaceAll('/', '_')
      .replace(/=+$/, '')
    expect(() => decodeSpec(encoded, 'estimate')).not.toThrow()
    expect(() => decodeSpec(nestedLink(5), 'estimate')).toThrow(/population has the wrong type/)
  })
})

describe('wrongly typed fields (review round 2)', () => {
  /** base64url of `spec` exactly as written (no type checks on the way in). */
  function link(spec: object): string {
    return btoa(JSON.stringify(spec))
      .replaceAll('+', '-')
      .replaceAll('/', '_')
      .replace(/=+$/, '')
  }
  const base = { activity: { preset: 'sleep' }, years: [2023] }
  const compareBase = {
    ...base,
    group_a: { sex: 'male' },
    group_b: { sex: 'female' },
    label_a: 'A',
    label_b: 'B',
  }

  // A wrongly typed value reached the page's rendering and event handlers:
  // `preset: 5` threw in humanize() (the app's error page), and a numeric
  // label_a made Analyze throw on labelA.trim().
  it.each([
    ['activity.preset a number', 'estimate', { ...base, activity: { preset: 5 } }],
    ['activity.preset an object', 'estimate', { ...base, activity: { preset: { a: 1 } } }],
    ['activity.include objects', 'estimate', { ...base, activity: { include: [{}] } }],
    ['activity.include a string', 'estimate', { ...base, activity: { include: '0101' } }],
    [
      'activity.exclude numbers',
      'estimate',
      { ...base, activity: { include: ['01'], exclude: [1] } },
    ],
    [
      'activity.label an object',
      'estimate',
      { ...base, activity: { include: ['01'], label: {} } },
    ],
    ['measure a number', 'estimate', { ...base, measure: 5 }],
    ['weights an object', 'trend', { ...base, weights: {} }],
    ['variance a list', 'estimate', { ...base, variance: [] }],
    ['confidence_level a string', 'estimate', { ...base, confidence_level: '0.95' }],
    ['population a number', 'estimate', { ...base, population: 5 }],
    ['population a list', 'estimate', { ...base, population: [] }],
    ['a population value an object', 'estimate', { ...base, population: { sex: {} } }],
    ['a population value a list', 'trend', { ...base, population: { age_min: [25] } }],
    ['label_a a number', 'compare', { ...compareBase, label_a: 7 }],
    ['label_b an object', 'compare', { ...compareBase, label_b: {} }],
    ['group_a a string', 'compare', { ...compareBase, group_a: 'men' }],
    ['a group value an object', 'compare', { ...compareBase, group_b: { sex: { x: 1 } } }],
  ] as const)('rejects a link with %s', (_label, operation, spec) => {
    expect(() => decodeSpec(link(spec), operation as Operation)).toThrow(InvalidShareLinkError)
  })

  // Review round 4: the API reads "no", "false", "0" and "off" as
  // has_household_children false; the page described any non-empty string
  // as "with household children", so the link was labelled with the
  // opposite of the population it computed.
  it.each([
    [
      'has_household_children "no"',
      'estimate',
      { ...base, population: { has_household_children: 'no' } },
    ],
    [
      'has_household_children "false"',
      'trend',
      { ...base, population: { has_household_children: 'false' } },
    ],
    [
      'has_household_children "0"',
      'estimate',
      { ...base, population: { has_household_children: '0' } },
    ],
    [
      'has_household_children 0',
      'estimate',
      { ...base, population: { has_household_children: 0 } },
    ],
    [
      'a shared has_household_children "off"',
      'compare',
      { ...compareBase, population: { has_household_children: 'off' } },
    ],
    [
      'group_a has_household_children "n"',
      'compare',
      { ...compareBase, group_a: { has_household_children: 'n' } },
    ],
    [
      'group_b has_household_children "false"',
      'compare',
      { ...compareBase, group_b: { has_household_children: 'false' } },
    ],
    ['age_min a string', 'estimate', { ...base, population: { age_min: '25' } }],
    ['region a boolean', 'estimate', { ...base, population: { region: true } }],
    ['sex a number', 'compare', { ...compareBase, group_a: { sex: 1 } }],
    ['state_fips a number', 'estimate', { ...base, population: { state_fips: 6 } }],
    [
      'diary_date_min a number',
      'estimate',
      { ...base, population: { diary_date_min: 20190101 } },
    ],
  ] as const)('rejects a population with %s (review round 4)', (_label, operation, spec) => {
    expect(() => decodeSpec(link(spec), operation as Operation)).toThrow(
      /(population|group_[ab]) has the wrong type/,
    )
  })

  it('accepts each population dimension with its own type, and null', () => {
    const population = {
      age_min: 25,
      age_max: 54,
      sex: 'female',
      employment_status: 'employed',
      has_household_children: false,
      region: 3,
      state_fips: '06',
      education_level: 'high_school',
      day_type: 'weekend',
      diary_date_min: '2019-01-01',
      diary_date_max: '2019-12-31',
    }
    expect(decodeSpec(link({ ...base, population }), 'estimate')).toEqual({
      ...base,
      population,
    })
    const groups = {
      ...compareBase,
      group_a: { has_household_children: true, region: null },
      group_b: { has_household_children: false },
    }
    expect(decodeSpec(link(groups), 'compare')).toEqual(groups)
    // A dimension the page does not know is left for the API to refuse (422).
    expect(() =>
      decodeSpec(link({ ...base, population: { shoe_size: '9' } }), 'estimate'),
    ).not.toThrow()
  })

  it('accepts every canonical spec the API returns and every starter link', () => {
    for (const [operation, spec] of [
      ['estimate', estimateSleepFixture.spec],
      ['estimate', estimatePandemicFixture.spec],
      ['trend', trendLeisureFixture.spec],
      ['estimate', compareChildrenFixture.group_a.spec],
    ] as const) {
      expect(() => decodeSpec(link(spec), operation)).not.toThrow()
    }
    const years = metaFixture.data.years
    for (const starter of STARTER_EXAMPLES) {
      const spec = starter.spec(years, Math.max(...years))
      expect(decodeSpec(encodeSpec(spec), starter.operation)).toEqual(spec)
    }
    // Unset optional fields may be null.
    expect(() =>
      decodeSpec(
        link({ ...compareBase, measure: null, label_a: null, population: { sex: null } }),
        'compare',
      ),
    ).not.toThrow()
  })
})
