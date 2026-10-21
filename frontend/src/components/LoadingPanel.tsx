/**
 * Accessible loading state. By default the panel is an `<output>` (implicit
 * `role="status"`), so assistive technology announces the message; the
 * visible text explains what is being computed (expensive multi-year
 * variance calculations can take ~10 s uncached, and the API provides no
 * progress signal — so none is invented).
 *
 * `announce={false}` is for a page that already announces its loading states
 * through a persistent live region of its own (the analysis page): the panel
 * is then a plain container, so each state is announced once, not twice.
 */
export function LoadingPanel({
  message,
  hint,
  announce = true,
}: {
  message: string
  hint?: string
  announce?: boolean
}) {
  const content = (
    <>
      <span className="spinner" aria-hidden="true" />
      <p style={{ margin: 0, fontWeight: 600 }}>{message}</p>
      {hint ? <p className="field-hint">{hint}</p> : null}
    </>
  )
  return announce ? (
    <output className="loading-panel">{content}</output>
  ) : (
    <div className="loading-panel">{content}</div>
  )
}
