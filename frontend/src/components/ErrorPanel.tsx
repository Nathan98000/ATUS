import { presentError } from '../domain/presentError'

interface ErrorPanelProps {
  error: unknown
  onRetry?: () => void
  /** Link target for "Adjust analysis" (when the request itself is the problem). */
  adjustHref?: string
}

/** Renders any API/network failure as an accessible, actionable message. */
export function ErrorPanel({ error, onRetry, adjustHref }: ErrorPanelProps) {
  const presented = presentError(error)
  return (
    <div className="error-banner" role="alert">
      <h2>{presented.heading}</h2>
      <p style={{ marginBottom: presented.hint ? '0.3rem' : 0 }}>{presented.message}</p>
      {presented.hint ? <p style={{ margin: 0 }}>{presented.hint}</p> : null}
      <p style={{ margin: '0.8rem 0 0', display: 'flex', gap: '0.6rem', flexWrap: 'wrap' }}>
        {presented.retryable && onRetry ? (
          <button type="button" className="btn" onClick={onRetry}>
            Try again
          </button>
        ) : null}
        {presented.adjustable && adjustHref ? (
          <a className="btn" href={adjustHref}>
            Adjust analysis
          </a>
        ) : null}
      </p>
      {presented.technical ? (
        <details>
          <summary>Technical details</summary>
          <p style={{ margin: '0.3rem 0 0' }}>
            <code>{presented.technical}</code>
          </p>
        </details>
      ) : null}
    </div>
  )
}
