/**
 * Methodological notes from the API's `warnings` array. Always rendered when
 * present — the API only sends warnings a reader needs in order to interpret
 * the result correctly (2020 collection windows, CI truncation, …).
 */
export function WarningBanner({ warnings }: { warnings: readonly string[] }) {
  if (warnings.length === 0) return null
  return (
    <div className="warning-banner" role="note" aria-label="Methodological notes">
      <p className="warning-banner__title" style={{ margin: 0 }}>
        <span aria-hidden="true">ⓘ</span> Note on interpreting this result
      </p>
      {warnings.length === 1 ? (
        <p style={{ margin: '0.25rem 0 0' }}>{warnings[0]}</p>
      ) : (
        <ul>
          {warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      )}
    </div>
  )
}
