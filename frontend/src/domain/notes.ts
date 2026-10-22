/**
 * Plain-language versions of the API's methodological notes.
 *
 * The API words its `warnings` and each trend year's `unavailable_reason`
 * for analysts (BLS variable names, replicate variance, the activity
 * lexicon). Result pages show every note in plain language instead. A note
 * this module does not recognize is shown exactly as the API wrote
 * it, as a caveat: a new note is never hidden or softened.
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

const PLAIN_NOTES: readonly {
  pattern: RegExp
  present: (match: RegExpMatchArray) => PresentedNote
}[] = [
  {
    pattern: /^Activity codes use the BLS-harmonized\b/,
    present: () => ({
      kind: 'context',
      text: 'Activities are coded the same way in every survey year, so years can be compared directly.',
    }),
  },
  {
    pattern: /^The difference's standard error uses per-replicate differences\b/,
    present: () => ({
      kind: 'context',
      text: "The difference's likely range allows for both groups coming from the same survey.",
    }),
  },
  {
    pattern: /^Pandemic weights\b/,
    present: () => ({
      kind: 'caveat',
      text: 'These estimates use the 2020 pandemic weights. They cover only the weeks when data collection ran normally (January 1 – March 17 and May 10 – December 31), so they are not full-year figures.',
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
      text: 'Weekday and weekend figures describe an average such day, not a share of the whole week.',
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

/** The notes that are context rather than caveats, in plain language. */
export function contextNotes(warnings: readonly string[]): string[] {
  return warnings
    .map(presentNote)
    .filter((note) => note.kind === 'context')
    .map((note) => note.text)
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
