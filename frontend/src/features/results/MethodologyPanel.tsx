/**
 * "How was this number calculated?" — a collapsible panel built from the
 * result's own metadata (weights, variance method, sample, versions), never
 * from hard-coded frontend claims. Links to the project's full statistical
 * documentation for depth.
 */
import type { ReactNode } from 'react'

export interface MethodologyRow {
  term: string
  detail: ReactNode
}

const ANALYTICS_DOCS_URL = 'https://github.com/Nathan98000/ATUS/blob/main/docs/analytics.md'

export function MethodologyPanel({
  rows,
  analysisKey,
}: {
  rows: MethodologyRow[]
  analysisKey: string | null
}) {
  return (
    <details className="panel">
      <summary>How was this calculated?</summary>
      <div className="panel__body">
        <dl className="methodology-list">
          {rows.map((row) => (
            <div key={row.term} style={{ display: 'contents' }}>
              <dt>{row.term}</dt>
              <dd>{row.detail}</dd>
            </div>
          ))}
          {analysisKey ? (
            <div style={{ display: 'contents' }}>
              <dt>Analysis key</dt>
              <dd>
                <code>{analysisKey}</code>
                <span className="field-hint" style={{ display: 'block' }}>
                  Deterministic identifier of this exact analysis (specification + statistical
                  implementation + data release). The same analysis always has the same key.
                </span>
              </dd>
            </div>
          ) : null}
        </dl>
        <p className="field-hint" style={{ marginTop: '0.7rem' }}>
          Full definitions of every term — weights, replicate variance, activity coding — are in
          the project's{' '}
          <a href={ANALYTICS_DOCS_URL} rel="noreferrer">
            statistical documentation
          </a>
          .
        </p>
      </div>
    </details>
  )
}
