import { Component, type ErrorInfo, type ReactNode } from 'react'

interface ErrorBoundaryState {
  failed: boolean
}

/**
 * Last-resort boundary for frontend programming errors (API problems are
 * handled by the pages). Shows a usable recovery screen instead of a blank
 * page; details go to the console for developers, never to the user.
 */
export class ErrorBoundary extends Component<{ children: ReactNode }, ErrorBoundaryState> {
  state: ErrorBoundaryState = { failed: false }

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { failed: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Unhandled application error', error, info.componentStack)
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <main style={{ maxWidth: '36rem', margin: '4rem auto', padding: '0 1rem' }}>
        <div className="error-banner" role="alert">
          <h2>Something went wrong in the application</h2>
          <p>
            This is a problem in ATUS Explorer itself, not in your analysis. Reloading the page
            usually recovers it.
          </p>
          <a className="btn" href="/">
            Reload ATUS Explorer
          </a>
        </div>
      </main>
    )
  }
}
