import { describe, expect, it } from 'vitest'

import { contextNotes, presentNote, presentUnavailableReason, shownWarnings } from './notes'

const HARMONIZED =
  'Activity codes use the BLS-harmonized 2003-25 multi-year lexicon (cross-year comparable by construction).'

describe('presentNote', () => {
  it('turns every note the API sends into plain language, keeping its numbers', () => {
    const notes = [
      "The difference's standard error uses per-replicate differences, which accounts for covariance between the group estimates.",
      'Pandemic weights (TU20FWGT): estimates represent only the comparable collection windows Jan 1 - Mar 17 and May 10 - Dec 31; annual estimates for 2020 are not possible.',
      'Pooled estimate: represents the average day across 4 survey years (1,461 days combined).',
      'Confidence interval truncated to [0, 1]; the normal approximation is poor for rates this close to a bound.',
      'Roster age is topcoded (80 in 2003-04, 85 from 2005), so age bounds of 80+ select topcoded groups, not exact ages.',
      'Education comes from the CPS interview 2-5 months before the diary day and excludes respondents with missing CPS education.',
      'Day-type estimates represent the average such day (weights are day-of-week calibrated), not a share of the overall week.',
    ]
    const presented = notes.map(presentNote)
    for (const [index, note] of presented.entries()) {
      expect(note.text).not.toBe(notes[index])
      expect(note.text).not.toMatch(/TU20FWGT|replicate|lexicon|CPS|topcoded/)
    }
    expect(presented.map((note) => note.kind)).toEqual([
      'context',
      'caveat',
      'caveat',
      'caveat',
      'caveat',
      'caveat',
      'caveat',
    ])
    expect(presented[2]?.text).toBe(
      'This is an average day across 4 survey years combined, not a single year.',
    )
  })

  it('leaves out the note that activity codes are harmonized (review round 5)', () => {
    const difference = "The difference's standard error uses per-replicate differences."
    expect(shownWarnings([HARMONIZED, difference])).toEqual([difference])
    expect(contextNotes([HARMONIZED])).toEqual([])
  })

  it('shows an unknown note verbatim, as a caveat', () => {
    expect(presentNote('Something new (ABCD).')).toEqual({
      kind: 'caveat',
      text: 'Something new (ABCD).',
    })
  })
})

describe('presentUnavailableReason', () => {
  it('explains the 2020 gap without variable names', () => {
    const text = presentUnavailableReason(
      "TUFNWGTP is undefined for 2020 (pandemic collection suspension); estimate 2020 separately with weights='pandemic'.",
    )
    expect(text).toMatch(/^Data collection paused in spring 2020 because of COVID-19/)
    expect(text).not.toMatch(/TUFNWGTP|weights=/)
  })

  it('explains an empty group, and keeps unknown reasons', () => {
    expect(presentUnavailableReason('no respondents match the population filters')).toBe(
      'No survey respondents match the selected group in this year.',
    )
    expect(presentUnavailableReason('Some other reason.')).toBe('Some other reason.')
    expect(presentUnavailableReason(null)).toBe('No estimate is available for this year.')
  })
})
