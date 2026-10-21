import { Link, useLocation } from 'react-router'

import { presentError } from '../domain/presentError'

/**
 * Full-page state for problems no single panel can fix: the free hosting
 * quota is exhausted (`database_paused`, `capacity_paused`, the edge's daily
 * request limit) or this page cannot talk to the service's API version.
 * One honest explanation replaces the page's content instead of a cascade of
 * error panels, says what still works, and offers the one useful action
 * (reload for an incompatible API; the home page and a retry otherwise).
 * Focus is not moved; the alert role announces it. The heading takes focus
 * only when a "Try again" lands here again (`useRetryFocus`).
 */
export function ServiceInterruption({
  error,
  onRetry,
}: {
  error: unknown
  onRetry?: () => void
}) {
  const presented = presentError(error)
  const { pathname } = useLocation()

  return (
    <section className="service-interruption" aria-labelledby="service-interruption-heading">
      <div className="error-banner" role="alert">
        <h1 id="service-interruption-heading" tabIndex={-1}>
          {presented.heading}
        </h1>
        <p>{presented.message}</p>
        {presented.hint ? <p>{presented.hint}</p> : null}
        <p className="service-interruption__actions">
          {presented.kind === 'incompatible' ? (
            <button type="button" className="btn" onClick={() => window.location.reload()}>
              Reload the page
            </button>
          ) : (
            <>
              {pathname !== '/' ? (
                <Link className="btn" to="/">
                  Go to the home page
                </Link>
              ) : null}
              {onRetry ? (
                <button type="button" className="btn" onClick={onRetry}>
                  Try again
                </button>
              ) : null}
            </>
          )}
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
    </section>
  )
}
