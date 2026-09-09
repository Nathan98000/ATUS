import { useEffect, useRef, useState } from 'react'

/**
 * Copies the current (shareable) URL. The URL alone reproduces the analysis —
 * no account, no server-side state.
 */
export function ShareButton() {
  const [state, setState] = useState<'idle' | 'copied' | 'manual'>('idle')
  const timer = useRef<ReturnType<typeof setTimeout>>(null)

  useEffect(() => {
    return () => {
      if (timer.current) clearTimeout(timer.current)
    }
  }, [])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href)
      setState('copied')
      if (timer.current) clearTimeout(timer.current)
      timer.current = setTimeout(() => setState('idle'), 2500)
    } catch {
      setState('manual')
    }
  }

  return (
    <span className="share-button">
      <button type="button" className="btn btn--small" onClick={copy}>
        Share analysis
      </button>
      <output className={state === 'copied' ? 'share-button__status' : 'visually-hidden'}>
        {state === 'copied' ? 'Link copied.' : ''}
      </output>
      {state === 'manual' ? (
        <input
          type="text"
          readOnly
          aria-label="Shareable analysis link — copy it manually"
          value={window.location.href}
          onFocus={(event) => event.target.select()}
          style={{ maxWidth: '16rem' }}
        />
      ) : null}
    </span>
  )
}
