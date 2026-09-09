/**
 * Accessible loading state. `role="status"` announces the message to
 * assistive technology; the visible text explains what is being computed
 * (expensive multi-year variance calculations can take ~10 s uncached, and
 * the API provides no progress signal — so none is invented).
 */
export function LoadingPanel({ message, hint }: { message: string; hint?: string }) {
  return (
    <output className="loading-panel">
      <span className="spinner" aria-hidden="true" />
      <p style={{ margin: 0, fontWeight: 600 }}>{message}</p>
      {hint ? <p className="field-hint">{hint}</p> : null}
    </output>
  )
}
