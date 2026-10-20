import { useEffect, useState } from 'react'

import { presentError } from '../domain/presentError'
import { ServiceInterruption } from './ServiceInterruption'

interface ErrorPanelProps {
  error: unknown
  onRetry?: () => void
  /** Link target for "Adjust analysis" (when the request itself is the problem). */
  adjustHref?: string
}

/**
 * Renders any API/network failure as an accessible, actionable message.
 * Paused quotas and an incompatible API are not panel-sized problems: they
 * render the full-page `ServiceInterruption` state instead.
 */
export function ErrorPanel({ error, onRetry, adjustHref }: ErrorPanelProps) {
  const presented = presentError(error)

  if (presented.kind === 'paused' || presented.kind === 'incompatible') {
    return <ServiceInterruption error={error} onRetry={onRetry} />
  }

  return (
    <div className="error-banner" role="alert">
      <h2>{presented.heading}</h2>
      <p style={{ marginBottom: presented.hint ? '0.3rem' : 0 }}>{presented.message}</p>
      {presented.detailLines.length > 0 ? (
        <ul style={{ margin: '0.2rem 0 0.4rem', paddingLeft: '1.2rem' }}>
          {presented.detailLines.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      ) : null}
      {presented.hint ? <p style={{ margin: 0 }}>{presented.hint}</p> : null}
      <p style={{ margin: '0.8rem 0 0', display: 'flex', gap: '0.6rem', flexWrap: 'wrap' }}>
        {presented.retryable && onRetry ? (
          // A new error restarts the wait: the key is the error's identity.
          <RetryButton
            key={errorIdentity(error)}
            waitSeconds={presented.retryAfterSeconds ?? 0}
            onRetry={onRetry}
          />
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

/**
 * "Try again", held back while the service's Retry-After runs. The ticking
 * seconds are hidden from assistive technology: the button sits inside a
 * live region, and the message above already says how long to wait.
 */
function RetryButton({ waitSeconds, onRetry }: { waitSeconds: number; onRetry: () => void }) {
  const [deadline] = useState(() => Date.now() + waitSeconds * 1000)
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (Date.now() >= deadline) return
    const timer = setInterval(() => {
      const current = Date.now()
      setNow(current)
      if (current >= deadline) clearInterval(timer)
    }, 1000)
    return () => clearInterval(timer)
  }, [deadline])

  const remaining = Math.max(0, Math.ceil((deadline - now) / 1000))
  return (
    <button type="button" className="btn" onClick={onRetry} disabled={remaining > 0}>
      Try again
      {remaining > 0 ? <span aria-hidden="true"> ({remaining} s)</span> : null}
    </button>
  )
}

const errorIds = new WeakMap<object, number>()
let lastErrorId = 0

/** A stable number per error object (errors are not otherwise keyable). */
function errorIdentity(error: unknown): number {
  if (typeof error !== 'object' || error === null) return 0
  let id = errorIds.get(error)
  if (id === undefined) {
    lastErrorId += 1
    id = lastErrorId
    errorIds.set(error, id)
  }
  return id
}
