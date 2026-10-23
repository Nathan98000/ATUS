import type { ReactNode } from 'react'

import { presentNote, shownWarnings } from '../domain/notes'

/**
 * Methodological notes from the API's `warnings` array, in plain language
 * (`domain/notes.ts`). Always rendered when present — the API only sends
 * notes a reader needs in order to interpret the result correctly. Caveats
 * about this particular result get the warning style here, inside the
 * result's card between the chart and its small print (review round 6: below
 * the card they sat after the source line); context true of every result of
 * its kind is a quieter line in the chart's small print (`contextNotes` in
 * domain/notes.ts, shown by each result view), so the warning style keeps
 * its meaning. `notes`: the page's own caveats (a figure resting on few
 * diaries), shown first.
 */
export function WarningBanner({
  warnings,
  notes = [],
}: {
  warnings: readonly string[]
  notes?: readonly ReactNode[]
}) {
  const caveats: ReactNode[] = [
    ...notes,
    ...shownWarnings(warnings)
      .map(presentNote)
      .filter((note) => note.kind === 'caveat')
      .map((note) => note.text),
  ]
  if (caveats.length === 0) return null
  return (
    <div className="result-notes" role="note" aria-label="Notes on this result">
      <div className="warning-banner">
        <p className="warning-banner__title" style={{ margin: 0 }}>
          <span aria-hidden="true">ⓘ</span> Keep in mind
        </p>
        {caveats.length === 1 ? (
          <p style={{ margin: '0.25rem 0 0' }}>{caveats[0]}</p>
        ) : (
          <ul>
            {caveats.map((note, index) => (
              <li key={index}>{note}</li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
