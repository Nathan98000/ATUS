import { presentNote } from '../domain/notes'

/**
 * Methodological notes from the API's `warnings` array, in plain language
 * (`domain/notes.ts`). Always rendered when present — the API only sends
 * notes a reader needs in order to interpret the result correctly. Caveats
 * about this particular result get the warning style; context true of every
 * result of its kind is shown as a quieter line, so the warning style keeps
 * its meaning. The API's exact wording is in the methodology panel.
 */
export function WarningBanner({ warnings }: { warnings: readonly string[] }) {
  if (warnings.length === 0) return null
  const notes = warnings.map(presentNote)
  const caveats = notes.filter((note) => note.kind === 'caveat')
  const context = notes.filter((note) => note.kind === 'context')
  return (
    <div className="result-notes" role="note" aria-label="Notes on this result">
      {caveats.length > 0 ? (
        <div className="warning-banner">
          <p className="warning-banner__title" style={{ margin: 0 }}>
            <span aria-hidden="true">ⓘ</span> Keep in mind
          </p>
          {caveats.length === 1 ? (
            <p style={{ margin: '0.25rem 0 0' }}>{caveats[0]?.text}</p>
          ) : (
            <ul>
              {caveats.map((note) => (
                <li key={note.text}>{note.text}</li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
      {context.map((note) => (
        <p key={note.text} className="context-note">
          {note.text}
        </p>
      ))}
    </div>
  )
}
