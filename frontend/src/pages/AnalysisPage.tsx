/**
 * Shareable analysis result page: /analysis/:operation?spec=<encoded spec>.
 *
 * The URL alone reconstructs the analysis (decode → validate → run → render).
 * After a successful estimate/trend run the URL is rewritten (replace, no
 * history entry) to encode the API's canonical spec, so equivalent requests
 * share one canonical link — mirroring how the backend itself identifies
 * analyses. The already-fetched result is seeded into the query cache under
 * the canonical key, so the rewrite never refetches.
 */
import { useEffect, useMemo } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'

import {
  analysisQueryKey,
  useAnalysis,
  useActivityPresets,
  useMeta,
  versionTag,
} from '../api/queries'
import type {
  AnalysisRequest,
  AnalysisResponse,
  CompareResponse,
  EstimateResponse,
  Operation,
  TrendResponse,
} from '../api/types'
import { isOperation } from '../api/types'
import { ErrorPanel } from '../components/ErrorPanel'
import { LoadingPanel } from '../components/LoadingPanel'
import { activityLabelFromSpec, analysisTitle, populationPhrase } from '../domain/describe'
import {
  InvalidShareLinkError,
  analysisPath,
  decodeSpec,
  encodeSpec,
  explorePath,
} from '../domain/urlSpec'
import { formatYears } from '../utils/format'
import { CompareView } from '../features/results/CompareView'
import { EstimateView } from '../features/results/EstimateView'
import { ShareButton } from '../features/results/ShareButton'
import { TrendView } from '../features/results/TrendView'

type ParsedLink = { invalid: string } | { operation: Operation; spec: AnalysisRequest }

export function AnalysisPage() {
  const params = useParams()
  const [searchParams] = useSearchParams()

  const parsed = useMemo((): ParsedLink => {
    const operation = params.operation
    if (!isOperation(operation)) {
      return { invalid: 'This analysis link points to an unknown analysis type.' }
    }
    const encoded = searchParams.get('spec')
    if (!encoded) {
      return { invalid: 'This analysis link is missing its specification.' }
    }
    try {
      return { operation, spec: decodeSpec(encoded, operation) }
    } catch (error) {
      return {
        invalid:
          error instanceof InvalidShareLinkError
            ? error.message
            : 'This analysis link is invalid or no longer supported.',
      }
    }
  }, [params.operation, searchParams])

  if ('invalid' in parsed) {
    return <InvalidLink message={parsed.invalid} />
  }
  return <AnalysisRunner operation={parsed.operation} spec={parsed.spec} />
}

function InvalidLink({ message }: { message: string }) {
  return (
    <div
      className="error-banner"
      role="alert"
      style={{ maxWidth: '36rem', margin: '2rem auto' }}
    >
      <h2>This analysis link cannot be opened</h2>
      <p>{message}</p>
      <p style={{ margin: 0 }}>
        <Link className="btn" to="/explore">
          Build an analysis instead
        </Link>
      </p>
    </div>
  )
}

const LOADING_MESSAGES: Record<Operation, string> = {
  estimate: 'Calculating estimate…',
  trend: 'Calculating trend and uncertainty…',
  compare: 'Comparing groups…',
}

function AnalysisRunner({ operation, spec }: { operation: Operation; spec: AnalysisRequest }) {
  const meta = useMeta()
  const presets = useActivityPresets()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const query = useAnalysis(operation, spec)

  const presetLabels = useMemo(() => {
    const map = new Map<string, string>()
    for (const preset of presets.data?.presets ?? []) map.set(preset.name, preset.label)
    return map
  }, [presets.data])

  const result: AnalysisResponse | undefined = query.data?.data
  const activityLabel = useMemo(() => {
    if (result) {
      if (operation === 'compare') return (result as CompareResponse).group_a.activity.label
      return (result as EstimateResponse | TrendResponse).activity.label
    }
    return activityLabelFromSpec(spec, presetLabels)
  }, [result, operation, spec, presetLabels])

  const title = analysisTitle(operation, spec, activityLabel)

  useEffect(() => {
    document.title = `${title} · ATUS Explorer`
    return () => {
      document.title = 'ATUS Explorer'
    }
  }, [title])

  // Canonicalize the URL from the API's own canonical spec (estimate/trend).
  useEffect(() => {
    if (!result || operation === 'compare' || !meta.data) return
    const canonical = (result as EstimateResponse | TrendResponse).spec as AnalysisRequest
    const canonicalEncoded = encodeSpec(canonical)
    if (canonicalEncoded === encodeSpec(spec)) return
    queryClient.setQueryData(
      analysisQueryKey(operation, canonical, versionTag(meta.data)),
      query.data,
    )
    navigate(`/analysis/${operation}?spec=${canonicalEncoded}`, { replace: true })
  }, [result, operation, spec, meta.data, navigate, query.data, queryClient])

  const adjustHref = explorePath(operation, spec)

  return (
    <div className="analysis-page">
      <header className="analysis-header">
        <div>
          <p className="eyebrow">
            {operation === 'estimate'
              ? 'Estimate'
              : operation === 'trend'
                ? 'Trend'
                : 'Comparison'}
          </p>
          <h1>{title}</h1>
          {operation !== 'compare' ? (
            <p className="field-hint">
              {populationPhrase(spec.population)} · {formatYears(spec.years)}
            </p>
          ) : null}
        </div>
        <div className="analysis-header__actions">
          <ShareButton />
          <Link className="btn btn--small" to={adjustHref}>
            Adjust analysis
          </Link>
          {operation === 'estimate' && meta.data && spec.weights !== 'pandemic' ? (
            // Only for the multi-year weights: the pandemic scheme covers
            // 2019–2020 only, so a full-period trend would always be refused.
            <Link
              className="btn btn--small"
              to={analysisPath('trend', {
                ...spec,
                years: meta.data.data.years,
              })}
            >
              View {formatYears(meta.data.data.years)} trend
            </Link>
          ) : null}
        </div>
      </header>

      {meta.isError ? (
        // The analysis query is version-keyed on /meta and stays disabled
        // while it has no version tag — a failing /meta must surface here,
        // or a cold deep link would show the loading state forever.
        <ErrorPanel error={meta.error} onRetry={() => meta.refetch()} />
      ) : query.isPending ? (
        <LoadingPanel
          message={LOADING_MESSAGES[operation]}
          hint={
            spec.variance !== 'none'
              ? 'Standard errors over long periods can take around ten seconds the first time; repeated analyses are nearly instant.'
              : undefined
          }
        />
      ) : query.isError ? (
        <ErrorPanel
          error={query.error}
          onRetry={() => query.refetch()}
          adjustHref={adjustHref}
        />
      ) : (
        <ResultBody
          operation={operation}
          result={query.data.data}
          analysisKey={query.data.analysisKey}
        />
      )}
      {/* Persistent live region: reliably announces the start and end of a
          potentially ~10 s computation (content mounted with its text
          already present is often not announced by screen readers). */}
      <p className="visually-hidden" aria-live="polite">
        {meta.isError || query.isError
          ? 'The analysis could not be completed.'
          : query.isPending
            ? LOADING_MESSAGES[operation]
            : 'Analysis ready.'}
      </p>
    </div>
  )
}

function ResultBody({
  operation,
  result,
  analysisKey,
}: {
  operation: Operation
  result: AnalysisResponse
  analysisKey: string | null
}) {
  if (operation === 'estimate') {
    return <EstimateView result={result as EstimateResponse} analysisKey={analysisKey} />
  }
  if (operation === 'trend') {
    return <TrendView result={result as TrendResponse} analysisKey={analysisKey} />
  }
  return <CompareView result={result as CompareResponse} analysisKey={analysisKey} />
}
