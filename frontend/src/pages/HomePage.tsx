/**
 * Landing page: what this is, where the data comes from, and immediate
 * entry points — a main call to action plus starter analyses that are real,
 * runnable specifications (linked as shareable analysis URLs built against
 * the years the API actually reports).
 */
import { useEffect } from 'react'
import { Link } from 'react-router'

import { useMeta } from '../api/queries'
import type { AnalysisRequest, Operation } from '../api/types'
import { analysisPath } from '../domain/urlSpec'

interface StarterExample {
  question: string
  detail: string
  operation: Operation
  spec: (years: number[], latest: number) => AnalysisRequest
}

const STARTER_EXAMPLES: StarterExample[] = [
  {
    question: 'How much do Americans sleep?',
    detail: 'Average time per day, most recent year',
    operation: 'estimate',
    spec: (_years, latest) => ({
      measure: 'average_minutes_per_day',
      activity: { preset: 'sleep' },
      years: [latest],
    }),
  },
  {
    question: 'How has leisure time changed?',
    detail: 'Yearly trend across the full period',
    operation: 'trend',
    spec: (years) => ({
      measure: 'average_minutes_per_day',
      activity: { preset: 'leisure_and_sports_bls_table' },
      years,
    }),
  },
  {
    question: 'Who watches more TV — men or women?',
    detail: 'Two groups and their difference, most recent year',
    operation: 'compare',
    spec: (_years, latest) => ({
      measure: 'average_minutes_per_day',
      activity: { preset: 'watching_tv' },
      years: [latest],
      group_a: { sex: 'male' },
      group_b: { sex: 'female' },
      label_a: 'Men',
      label_b: 'Women',
    }),
  },
  {
    question: 'Does household work differ with children at home?',
    detail: 'Households with vs without children under 18',
    operation: 'compare',
    spec: (_years, latest) => ({
      measure: 'average_minutes_per_day',
      activity: { preset: 'household_activities_bls_table' },
      years: [latest],
      group_a: { has_household_children: true },
      group_b: { has_household_children: false },
      label_a: 'Household children present',
      label_b: 'No household children',
    }),
  },
]

export function HomePage() {
  const meta = useMeta()

  useEffect(() => {
    document.title = 'ATUS Explorer'
  }, [])

  const years = meta.data?.data.years ?? []
  const latest = years.length > 0 ? Math.max(...years) : null

  return (
    <div className="home-page">
      <section className="home-hero">
        <h1>Explore how Americans spend their time.</h1>
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
        <h2 id="starter-heading">Start from a question</h2>
        {latest !== null ? (
          <div className="example-grid">
            {STARTER_EXAMPLES.map((example) => (
              <Link
                key={example.question}
                className="example-card"
                to={analysisPath(example.operation, example.spec(years, latest))}
              >
                <span className="example-card__question">{example.question}</span>
                <span className="field-hint">{example.detail}</span>
              </Link>
            ))}
          </div>
        ) : (
          <output className="field-hint" style={{ display: 'block' }}>
            {meta.isError
              ? 'Starter examples need the analysis service, which could not be reached.'
              : 'Loading starter examples…'}
          </output>
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
