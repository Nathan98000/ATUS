/**
 * Shareable analysis result page: /analysis/:operation?spec=<encoded spec>,
 * plus `&split=…&groups=…` for a trend split into one line per group (one
 * point-estimate trend request per group; such links are not rewritten).
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
  refetchFailed,
  useAnalyses,
  useAnalysis,
  useActivityPresets,
  useMeta,
  usePopulationMetadata,
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
import {
  activityLabelFromSpec,
  analysisTitle,
  definitionNote,
  resultHeading,
} from '../domain/describe'
import { isFullPageError } from '../domain/presentError'
import {
  InvalidShareLinkError,
  analysisPath,
  decodeSpec,
  encodeSpec,
  explorePath,
  parseSplit,
  type SplitSelection,
} from '../domain/urlSpec'
import { resolveSplit, seriesSpec } from '../features/population/compareGroups'
import { formatYears } from '../utils/format'
import { CompareView } from '../features/results/CompareView'
import { EstimateView } from '../features/results/EstimateView'
import { ShareButton } from '../features/results/ShareButton'
import { SplitTrendView, TrendView, type SplitSeries } from '../features/results/TrendView'

type ParsedLink =
  | { invalid: string }
  | { operation: Operation; spec: AnalysisRequest; split: SplitSelection | null }

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
      return {
        operation,
        spec: decodeSpec(encoded, operation),
        split: parseSplit(searchParams, operation),
      }
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
  return <AnalysisRunner operation={parsed.operation} spec={parsed.spec} split={parsed.split} />
}

function InvalidLink({ message }: { message: string }) {
  return (
    <div
      className="error-banner"
      role="alert"
      style={{ maxWidth: '36rem', margin: '2rem auto' }}
    >
      <h1 tabIndex={-1}>This analysis link cannot be opened</h1>
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
const SPLIT_LOADING_MESSAGE = 'Calculating a trend for each group…'
const UNKNOWN_SPLIT =
  'This link splits the trend into groups that this data release does not offer.'

/** Before /meta arrives no analysis has been sent, so nothing is "calculating" yet (audit F11). */
const PREPARING_MESSAGE = 'Preparing the analysis…'
const WAKING_LOADING_MESSAGE = 'Waiting for the analysis service to wake up…'

function AnalysisRunner({
  operation,
  spec,
  split,
}: {
  operation: Operation
  spec: AnalysisRequest
  split: SplitSelection | null
}) {
  const meta = useMeta()
  const versions = useVersionTag()
  const presets = useActivityPresets()
  const populationMetadata = usePopulationMetadata()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  // A split trend runs one trend per group instead of the spec itself.
  const resolved =
    split !== null && populationMetadata.data !== undefined
      ? resolveSplit(populationMetadata.data.dimensions, split)
      : null
  const single = useAnalysis(operation, split === null ? spec : null)
  const seriesQueries = useAnalyses(
    'trend',
    resolved ? resolved.options.map((option) => seriesSpec(spec, resolved, option)) : [],
  )
  const query = useCombinedQuery(single, split === null ? null : seriesQueries)
  const splitUnusable =
    split !== null && populationMetadata.data !== undefined && resolved === null
  const waking = useServiceWaking()
  // "Try again" keeps focus on the page (the panel holding the button unmounts).
  const retry = useRetryFocus(
    meta.isFetching || query.isFetching || populationMetadata.isFetching,
  )

  const presetLabels = useMemo(() => {
    const map = new Map<string, string>()
    for (const preset of presets.data?.presets ?? []) map.set(preset.name, preset.label)
    return map
  }, [presets.data])

  const result: AnalysisResponse | undefined = query.data?.data ?? query.series?.[0]
  const activityLabel = useMemo(() => {
    if (result) {
      if (operation === 'compare') return (result as CompareResponse).group_a.activity.label
      return (result as EstimateResponse | TrendResponse).activity.label
    }
    return activityLabelFromSpec(spec, presetLabels)
  }, [result, operation, spec, presetLabels])

  const splitPhrase = resolved ? `by ${resolved.choice.label.toLowerCase()}` : null
  const title = [analysisTitle(operation, spec, activityLabel), splitPhrase]
    .filter(Boolean)
    .join(' — ')
  // The subtitle names a comparison's shared filters: they restrict both
  // groups and must be named with the result, not only inside its collapsed
  // panels (review round 3).
  const heading = resultHeading(operation, spec, activityLabel)
  if (resolved !== null) {
    const groups = resolved.options.map((option) => option.label)
    heading.eyebrow = `${heading.eyebrow} and ${resolved.choice.label.toLowerCase()}`
    heading.subtitle = `${heading.subtitle} · ${
      groups.length <= 3 ? groups.join(', ') : `${groups.length} groups`
    }`
  }

  useEffect(() => {
    document.title = `${title} · ATUS Explorer`
    return () => {
      document.title = 'ATUS Explorer'
    }
  }, [title])

  // Canonicalize the URL from the API's own canonical spec (estimate/trend;
  // a split trend's link names its groups and stays as written).
  useEffect(() => {
    if (!result || operation === 'compare' || split !== null || versions === null) return
    const canonical = (result as EstimateResponse | TrendResponse).spec as AnalysisRequest
    const canonicalEncoded = encodeSpec(canonical)
    if (canonicalEncoded === encodeSpec(spec)) return
    queryClient.setQueryData(analysisQueryKey(operation, canonical, versions), query.data)
    navigate(`/analysis/${operation}?spec=${canonicalEncoded}`, { replace: true })
  }, [result, operation, spec, split, versions, navigate, query.data, queryClient])

  const adjustHref = explorePath(operation, spec, split)
  // Only a /meta failure with nothing loaded blocks the page; a failed
  // background refetch never hides a displayed result (audit F26).
  const metaFailed =
    (meta.data === undefined && meta.isError) ||
    (split !== null && populationMetadata.data === undefined && populationMetadata.isError)
  const metaError = meta.isError ? meta.error : populationMetadata.error
  const blockingError = metaFailed ? metaError : query.isError ? query.error : null
  // What the page announces while it loads: the site-wide notice already
  // announces the waking state, so the persistent live region below keeps
  // the operation's own message and only the visible text says "waking".
  const announcedLoadingMessage =
    meta.data === undefined
      ? PREPARING_MESSAGE
      : split !== null
        ? SPLIT_LOADING_MESSAGE
        : LOADING_MESSAGES[operation]
  const loadingMessage =
    meta.data !== undefined && waking ? WAKING_LOADING_MESSAGE : announcedLoadingMessage

  const retryMeta = () => retry(() => refetchFailed(meta, populationMetadata))

  if (splitUnusable) return <InvalidLink message={UNKNOWN_SPLIT} />
  // A paused quota or an incompatible API replaces the whole page.
  if (blockingError !== null && isFullPageError(blockingError)) {
    return (
      <ServiceInterruption
        error={blockingError}
        onRetry={metaFailed ? retryMeta : () => retry(() => query.refetch())}
      />
    )
  }

  return (
    <div className="analysis-page">
      <header className="analysis-header">
        <div>
          <p className="eyebrow">{heading.eyebrow}</p>
          <h1 tabIndex={-1}>{heading.title}</h1>
          <p className="analysis-header__details">{heading.subtitle}</p>
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
        <ErrorPanel error={metaError} onRetry={retryMeta} />
      ) : meta.data === undefined || query.isPending ? (
        // Not a live region itself: the persistent one below announces each
        // state once (a second region with the same text is read twice).
        <LoadingPanel
          announce={false}
          message={loadingMessage}
          hint={
            meta.data !== undefined && !waking && split === null && spec.variance !== 'none'
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
      ) : query.series !== undefined && resolved ? (
        <SplitTrendView
          series={resolved.options.map((option, index): SplitSeries => ({
            label: option.label,
            result: query.series?.[index] as TrendResponse,
          }))}
          legendTitle={resolved.choice.label}
          note={definitionNote(operation, spec, presets.data?.presets)}
        />
      ) : query.data !== undefined ? (
        <ResultBody
          operation={operation}
          result={query.data.data}
          note={definitionNote(operation, spec, presets.data?.presets)}
        />
      ) : null}
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
  note,
}: {
  operation: Operation
  result: AnalysisResponse
  note: string | null
}) {
  if (operation === 'estimate') {
    return <EstimateView result={result as EstimateResponse} note={note} />
  }
  if (operation === 'trend') {
    return <TrendView result={result as TrendResponse} note={note} />
  }
  return <CompareView result={result as CompareResponse} note={note} />
}

/**
 * One analysis, or a split trend's analyses as one: pending while any is,
 * failed (with the first failure) when any failed, and "Try again" refetches
 * every failed one. `series` holds the split's results, in group order.
 */
function useCombinedQuery(
  single: ReturnType<typeof useAnalysis>,
  many: ReturnType<typeof useAnalyses<'trend'>> | null,
) {
  if (many === null) {
    return {
      isPending: single.isPending,
      isFetching: single.isFetching,
      isError: single.isError,
      error: single.error,
      refetch: single.refetch,
      data: single.data,
      series: undefined,
    }
  }
  const failed = many.find((entry) => entry.isError)
  const done = many.length > 0 && many.every((entry) => entry.data !== undefined)
  return {
    isPending: !done && failed === undefined,
    isFetching: many.some((entry) => entry.isFetching),
    isError: failed !== undefined,
    error: failed?.error ?? null,
    refetch: () => {
      refetchFailed(...many)
      return Promise.resolve()
    },
    data: undefined,
    series: done ? many.map((entry) => entry.data?.data as TrendResponse) : undefined,
  }
}
