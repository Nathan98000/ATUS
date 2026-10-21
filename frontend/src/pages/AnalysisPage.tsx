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
  useLoadedDataVersion,
  useMeta,
  useVersionTag,
} from '../api/queries'
import { useServiceWaking } from '../api/serviceStatus'
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
import { ServiceInterruption } from '../components/ServiceInterruption'
import { useRetryFocus } from '../components/useRetryFocus'
import { activityLabelFromSpec, analysisTitle, populationPhrase } from '../domain/describe'
import { isFullPageError } from '../domain/presentError'
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
import { ResultDownloads } from '../features/results/ResultDownloads'
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

/** Before /meta arrives no analysis has been sent, so nothing is "calculating" yet (audit F11). */
const PREPARING_MESSAGE = 'Preparing the analysis…'
const WAKING_LOADING_MESSAGE = 'Waiting for the analysis service to wake up…'

function AnalysisRunner({ operation, spec }: { operation: Operation; spec: AnalysisRequest }) {
  const meta = useMeta()
  const versions = useVersionTag()
  const loadedDataVersion = useLoadedDataVersion()
  const presets = useActivityPresets()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const query = useAnalysis(operation, spec)
  const waking = useServiceWaking()
  // "Try again" keeps focus on the page (the panel holding the button unmounts).
  const retry = useRetryFocus(meta.isFetching || query.isFetching)

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
    if (!result || operation === 'compare' || versions === null) return
    const canonical = (result as EstimateResponse | TrendResponse).spec as AnalysisRequest
    const canonicalEncoded = encodeSpec(canonical)
    if (canonicalEncoded === encodeSpec(spec)) return
    queryClient.setQueryData(analysisQueryKey(operation, canonical, versions), query.data)
    navigate(`/analysis/${operation}?spec=${canonicalEncoded}`, { replace: true })
  }, [result, operation, spec, versions, navigate, query.data, queryClient])

  const adjustHref = explorePath(operation, spec)
  // Only a /meta failure with nothing loaded blocks the page; a failed
  // background refetch never hides a displayed result (audit F26).
  const metaFailed = meta.data === undefined && meta.isError
  const blockingError = metaFailed ? meta.error : query.isError ? query.error : null
  // What the page announces while it loads: the site-wide notice already
  // announces the waking state, so the persistent live region below keeps
  // the operation's own message and only the visible text says "waking".
  const announcedLoadingMessage =
    meta.data === undefined ? PREPARING_MESSAGE : LOADING_MESSAGES[operation]
  const loadingMessage =
    meta.data !== undefined && waking ? WAKING_LOADING_MESSAGE : announcedLoadingMessage

  // A paused quota or an incompatible API replaces the whole page.
  if (blockingError !== null && isFullPageError(blockingError)) {
    return (
      <ServiceInterruption
        error={blockingError}
        onRetry={() => retry(() => (metaFailed ? meta.refetch() : query.refetch()))}
      />
    )
  }

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
          <h1 tabIndex={-1}>{title}</h1>
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

      {metaFailed ? (
        // The analysis query is version-keyed on /meta and stays disabled
        // while it has no version tag — a failing /meta must surface here,
        // or a cold deep link would show the loading state forever.
        <ErrorPanel error={meta.error} onRetry={() => retry(() => meta.refetch())} />
      ) : meta.data === undefined || query.isPending ? (
        // Not a live region itself: the persistent one below announces each
        // state once (a second region with the same text is read twice).
        <LoadingPanel
          announce={false}
          message={loadingMessage}
          hint={
            meta.data !== undefined && !waking && spec.variance !== 'none'
              ? 'Standard errors over many years can take up to a minute the first time; repeated analyses are nearly instant.'
              : undefined
          }
        />
      ) : query.isError ? (
        <ErrorPanel
          error={query.error}
          onRetry={() => retry(() => query.refetch())}
          adjustHref={adjustHref}
        />
      ) : (
        <>
          <ResultBody
            operation={operation}
            result={query.data.data}
            analysisKey={query.data.analysisKey}
          />
          <ResultDownloads
            operation={operation}
            requestSpec={spec}
            result={query.data.data}
            analysisKey={query.data.analysisKey}
            // The release that produced this response; the page's own if it did not say.
            dataVersion={query.data.dataVersion ?? loadedDataVersion}
            apiVersion={meta.data.api_version}
            dataRelease={meta.data.data.release}
          />
        </>
      )}
      {/* Persistent live region: reliably announces the start and end of a
          potentially ~10 s computation (content mounted with its text
          already present is often not announced by screen readers). */}
      <p className="visually-hidden" aria-live="polite">
        {metaFailed || query.isError
          ? 'The analysis could not be completed.'
          : meta.data === undefined || query.isPending
            ? announcedLoadingMessage
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
