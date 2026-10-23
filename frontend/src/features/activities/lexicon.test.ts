import { describe, expect, it } from 'vitest'

import { activitiesFixture } from '../../test/fixtures'
import { buildLexiconIndex } from './lexicon'

const index = buildLexiconIndex(activitiesFixture.activities)

describe('lexicon index (built from the real 556-entry lexicon)', () => {
  it('indexes every entry and the top level', () => {
    expect(index.entries.length).toBe(activitiesFixture.total)
    expect(index.topLevel.length).toBeGreaterThan(10)
    expect(index.topLevel.every((entry) => entry.level === 1)).toBe(true)
  })

  it('resolves children and ancestor paths', () => {
    const sleeping = index.byCode.get('0101')
    expect(sleeping?.name).toBe('Sleeping')
    expect(index.childrenOf.get('01')?.some((entry) => entry.code === '0101')).toBe(true)
    expect(index.pathOf('010101').map((entry) => entry.code)).toEqual(['01', '0101'])
  })

  it('counts six-digit descendants (presentation only)', () => {
    expect(index.leafCount('0101')).toBe(3) // sleeping, sleeplessness, sleeping n.e.c.
    expect(index.leafCount('010101')).toBe(1)
    expect(index.leafCount('12')).toBeGreaterThan(10)
  })

  it('names what a selection includes, counting a listed catch-all unnamed (review round 6)', () => {
    expect(index.included(['050101', '050102', '050189'])).toEqual({
      names: ['Work, main job', 'Work, other job(s)'],
      unnamed: 1,
    })
    expect(index.included(['0101']).names).toEqual(['Sleeping', 'Sleeplessness'])
  })

  it('searches by name and code, prefix matches first', () => {
    const byName = index.search('television')
    expect(byName.length).toBeGreaterThan(0)
    expect(byName[0]?.name.toLowerCase()).toContain('television')

    const byCode = index.search('1203')
    expect(byCode[0]?.code.startsWith('1203')).toBe(true)

    expect(index.search('')).toEqual([])
    expect(index.search('zzzz-no-such-activity')).toEqual([])
  })
})
