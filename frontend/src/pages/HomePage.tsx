/**
 * Landing page: what this is, a chart that shows it (a trend split by sex,
 * from the deployed snapshot), where the data comes from, and immediate
 * entry points — a main call to action plus starter analyses that are real,
 * runnable specifications (linked as shareable analysis URLs built against
 * the years the API actually reports), each previewing its answer when the
 * deployed snapshot holds it.
 */
import { useEffect } from 'react'
import { Link } from 'react-router'

import { useMeta } from '../api/queries'
import { AnalysisTypeIcon } from '../components/AnalysisTypeIcon'
import { ServiceInterruption } from '../components/ServiceInterruption'
import { useRetryFocus } from '../components/useRetryFocus'
import { isFullPageError } from '../domain/presentError'
import { STARTER_EXAMPLES } from '../domain/prewarm'
import { analysisPath } from '../domain/urlSpec'
import { HomeChart } from '../features/home/HomeChart'
import { StarterPreview } from '../features/home/StarterPreview'

const STARTER_ICONS = { estimate: 'number', trend: 'line', compare: 'bars' } as const

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
        {years.length > 0 ? <HomeChart years={years} /> : null}
        <p className="home-hero__lead">
          Every year, the{' '}
          <a href="https://www.bls.gov/tus/" rel="noreferrer">
            American Time Use Survey
          </a>{' '}
          asks thousands of people to walk through one full day, minute by minute. See what
          those diaries say about sleep, work, chores, TV and more — for everyone, or for the
          groups you choose{meta.data ? `, from ${years[0]} to ${years[years.length - 1]}` : ''}
          .
        </p>
        <p className="home-hero__action">
          <Link to="/explore" className="btn btn--primary btn--large">
            Make your own chart
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
                  <AnalysisTypeIcon kind={STARTER_ICONS[example.operation]} />
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
        <h2>Good to know</h2>
        <ul>
          <li>
            <b>The figures stand for everyone.</b> Each diary is weighted so the results
            represent all Americans age 15 and over, across every day of the week and the year.
          </li>
          <li>
            <b>They are estimates.</b> The survey asks a sample of people, so charts show a
            likely range around each figure: a shaded band or thin lines.
          </li>
          <li>
            <b>2020 is a partial year.</b> The survey paused in spring 2020 because of COVID-19,
            so 2020 figures cover the rest of the year; charts draw them as a hollow point.
          </li>
        </ul>
        <p className="field-hint">
          More about the survey and the methods on the <Link to="/about">About page</Link>.
        </p>
      </section>
    </div>
  )
}
