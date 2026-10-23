/**
 * Plain-language versions of the API's methodological notes.
 *
 * The API words its `warnings` and each trend year's `unavailable_reason`
 * for analysts (BLS variable names, replicate variance, the activity
 * lexicon). Result pages show every note in plain language instead. A note
 * this module does not recognize is shown exactly as the API wrote
 * it, as a caveat: a new note is never hidden or softened. The one note
 * left out is that activity codes are harmonized across years (review round
 * 5): it is true of every result and says nothing about this one.
 */

/**
 * `caveat`: changes how this particular number should be read (pooled years,
 * pandemic weights, a truncated interval…). `context`: true of every result
 * of its kind (the activity coding, how a difference's uncertainty is
 * computed) — still shown, but without a warning's emphasis.
 */
export type NoteKind = 'caveat' | 'context'

export interface PresentedNote {
  kind: NoteKind
  text: string
}

/** The API's note that activity codes are harmonized across years: not shown. */
const OMITTED_NOTE = /^Activity codes use the BLS-harmonized\b/

const PLAIN_NOTES: readonly {
  pattern: RegExp
  present: (match: RegExpMatchArray) => PresentedNote
}[] = [
  {
    pattern: /^The difference's standard error uses per-replicate differences\b/,
    present: () => ({
      kind: 'context',
      text: "The difference's interval allows for both groups coming from the same survey.",
    }),
  },
  {
    pattern: /^Pandemic weights\b/,
    present: () => ({
      kind: 'caveat',
      // In plain words, without "weights" (review round 6); true of 2020
      // alone and of 2019 with 2020, the only years these weights cover.
      text: '2020 is a partial year. These figures cover only the weeks the survey ran normally (January 1 – March 17 and May 10 – December 31), so they are not full-year figures.',
    }),
  },
  {
    pattern: /^Pooled estimate: represents the average day across (\d+) survey years\b/,
    present: (match) => ({
      kind: 'caveat',
      text: `This is an average day across ${match[1]} survey years combined, not a single year.`,
    }),
  },
  {
    pattern: /^Confidence interval truncated to \[0, 1\]/,
    present: () => ({
      kind: 'caveat',
      text: 'The confidence interval was cut off at 0% or 100%. For rates this close to the edge, it is only approximate.',
    }),
  },
  {
    pattern: /^Roster age is topcoded\b/,
    present: () => ({
      kind: 'caveat',
      text: 'The survey records the oldest ages as a group (80 and over in 2003–04, 85 and over since 2005), so age limits of 80 or more select those groups, not exact ages.',
    }),
  },
  {
    pattern: /^Education comes from the CPS interview\b/,
    present: () => ({
      kind: 'caveat',
      text: 'Education comes from an earlier interview, 2–5 months before the diary day. People with no education on record are left out.',
    }),
  },
  {
    pattern: /^Day-type estimates represent the average such day\b/,
    present: () => ({
      kind: 'caveat',
      text: 'Each figure is for an average weekday or an average weekend day, not the whole week.',
    }),
  },
]

/** One API warning in plain language (unrecognized: verbatim, as a caveat). */
export function presentNote(warning: string): PresentedNote {
  for (const { pattern, present } of PLAIN_NOTES) {
    const match = warning.match(pattern)
    if (match) return present(match)
  }
  return { kind: 'caveat', text: warning }
}

/** The API's notes a result page shows (all but the omitted one). */
export function shownWarnings(warnings: readonly string[]): string[] {
  return warnings.filter((warning) => !OMITTED_NOTE.test(warning))
}

/** The notes that are context rather than caveats, in plain language. */
export function contextNotes(warnings: readonly string[]): string[] {
  return shownWarnings(warnings)
    .map(presentNote)
    .filter((note) => note.kind === 'context')
    .map((note) => note.text)
}

/**
 * Fewer diaries than this behind a figure, and the page says it is too few
 * to rely on (review round 6, the owner's decision D7). A choice of the
 * site's, not a rule of the survey's: the number is the API's own
 * `n_respondents`, and nothing is computed from it.
 */
export const FEW_DIARIES = 100

/**
 * The caution for a figure resting on few diaries, or null. `participants`:
 * how many of them did the activity — none explains an interval of 0 to 0
 * (every replicate estimate is zero), which would otherwise read as
 * certainty. `who` names the group for a comparison's bar.
 */
export function fewDiariesNote(
  diaries: number,
  participants?: number | null,
  who?: string,
): string | null {
  if (diaries >= FEW_DIARIES) return null
  const behind = who === undefined ? 'this figure' : `the bar “${who}”`
  let text = `Only ${diaries.toLocaleString('en-US')} people's diaries are behind ${behind}, too few to rely on. With so few, the 95% confidence interval can understate the uncertainty.`
  if (participants === 0 && diaries > 0) {
    text += ' None of them did this on their diary day, which is why the interval is 0 to 0.'
  }
  return text
}

/**
 * The same caution for a trend whose years rest on few diaries, or null:
 * `counts` are the drawn points' `n_respondents`; `band` says whether the
 * chart draws a confidence band (a split trend draws none).
 */
export function fewDiariesTrendNote(
  counts: readonly (number | null | undefined)[],
  band: boolean,
): string | null {
  if (!counts.some((count) => count != null && count < FEW_DIARIES)) return null
  return `Some points rest on fewer than ${FEW_DIARIES} people's diaries, too few to rely on.${
    band ? ' With so few, the shaded band can understate the uncertainty there.' : ''
  }`
}

/** Why a trend year has no estimate, in plain language (unrecognized: verbatim). */
export function presentUnavailableReason(reason: string | null | undefined): string {
  if (reason == null || reason.trim() === '') return 'No estimate is available for this year.'
  if (/^TUFNWGTP is undefined for 2020\b/.test(reason)) {
    return 'Data collection paused in spring 2020 because of COVID-19, so there is no full-year estimate comparable with other years.'
  }
  if (/^no respondents match the population filters\b/i.test(reason)) {
    return 'No survey respondents match the selected group in this year.'
  }
  return reason
}

/** The same reason in a few words, for a chart's tooltip (unrecognized: verbatim). */
export function shortUnavailableReason(reason: string | null | undefined): string {
  if (reason != null && /^TUFNWGTP is undefined for 2020\b/.test(reason)) {
    return 'the survey paused in spring 2020 (COVID-19).'
  }
  if (reason != null && /^no respondents match the population filters\b/i.test(reason)) {
    return 'no survey respondents in this group that year.'
  }
  return presentUnavailableReason(reason)
}
