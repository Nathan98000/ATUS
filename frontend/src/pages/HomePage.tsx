/**
 * Landing page: what this is, where the data comes from, and immediate
 * entry points — a main call to action plus starter analyses that are real,
 * runnable specifications (linked as shareable analysis URLs built against
 * the years the API actually reports), each previewing its answer when the
 * deployed snapshot holds it.
 */
import { useEffect } from 'react'
import { Link } from 'react-router'

import { useMeta } from '../api/queries'
import { ServiceInterruption } from '../components/ServiceInterruption'
import { useRetryFocus } from '../components/useRetryFocus'
import { isFullPageError } from '../domain/presentError'
import { STARTER_EXAMPLES } from '../domain/prewarm'
import { analysisPath } from '../domain/urlSpec'
import { StarterPreview } from '../features/home/StarterPreview'

export function HomePage() {
  const meta = useMeta()
  const retry = useRetryFocus(meta.isFetching)

  useEffect(() => {
    document.title = 'ATUS Explorer'
  }, [])

  const years = meta.data?.data.years ?? []
  const latest = years.length > 0 ? Math.max(...years) : null

  // A paused quota or an incompatible API is one honest page, not a broken home page.
  if (meta.data === undefined && meta.isError && isFullPageError(meta.error)) {
    return (
      <ServiceInterruption error={meta.error} onRetry={() => retry(() => meta.refetch())} />
    )
  }

  return (
    <div className="home-page">
      <section className="home-hero">
        <h1 tabIndex={-1}>Explore how Americans spend their time.</h1>
        <p style={{ maxWidth: '44rem' }}>
          Every year, the{' '}
          <a href="https://www.bls.gov/tus/" rel="noreferrer">
            American Time Use Survey
          </a>{' '}
          (U.S. Bureau of Labor Statistics) asks thousands of people to recount one full day —
          minute by minute. This explorer turns those diaries into population estimates: how
          much time people spend sleeping, working, caring for others, or watching TV, for any
          group you define{meta.data ? `, from ${years[0]} to ${years[years.length - 1]}` : ''}.
        </p>
        <p style={{ maxWidth: '44rem' }}>
          Every number is survey-weighted and shown with its sampling uncertainty — an estimate,
          not a headcount.
        </p>
        <p>
          <Link to="/explore" className="btn btn--primary">
            Build your own analysis
          </Link>
        </p>
      </section>

      <section aria-labelledby="starter-heading" className="home-examples">
        <h2 id="starter-heading" tabIndex={-1}>
          Start from a question
        </h2>
        {latest !== null ? (
          <div className="example-grid">
            {STARTER_EXAMPLES.map((example) => {
              const spec = example.spec(years, latest)
              return (
                <Link
                  key={example.question}
                  className="example-card"
                  to={analysisPath(example.operation, spec)}
                >
                  <span className="example-card__question">{example.question}</span>
                  <span className="field-hint">{example.detail}</span>
                  <StarterPreview operation={example.operation} spec={spec} />
                </Link>
              )
            })}
          </div>
        ) : (
          <>
            <output className="field-hint" style={{ display: 'block' }}>
              {meta.isError
                ? 'Starter examples need the analysis service, which could not be reached.'
                : 'Loading starter examples…'}
            </output>
            {meta.isError ? (
              <p style={{ marginTop: '0.6rem' }}>
                <button
                  type="button"
                  className="btn btn--small"
                  onClick={() => retry(() => meta.refetch(), '#starter-heading')}
                >
                  Try again
                </button>
              </p>
            ) : null}
          </>
        )}
      </section>

      <section className="home-notes">
        <h2>What you are looking at</h2>
        <ul>
          <li>
            <b>Weighted estimates.</b> Survey weights make each diary stand in for the people it
            represents, balanced across days of the week and the year.
          </li>
          <li>
            <b>Uncertainty, always.</b> Standard errors and confidence intervals use the
            survey's official replicate weights.
          </li>
          <li>
            <b>2020 is special.</b> Data collection was suspended in spring 2020; the explorer
            shows 2020 as an explicit gap or under its dedicated methodology — never smoothed
            over.
          </li>
        </ul>
        <p className="field-hint">
          More on the methodology on the <Link to="/about">About page</Link>.
        </p>
      </section>
    </div>
  )
}
