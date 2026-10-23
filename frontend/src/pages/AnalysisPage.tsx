/**
 * Shareable analysis result page: /analysis/:operation?spec=<encoded spec>,
 * plus `&split=…&groups=…` for a trend split into one line per group (one
 * point-estimate trend request per group; such links are not rewritten).
 *
 * The URL alone reconstructs the analysis (decode → validate → run → render),
 * under the site's own weights and 95% confidence interval
 * (`withAnalysisSettings`): a link written with other settings is rewritten
 * to them before it runs. After a successful estimate/trend run the URL is
 * rewritten (replace, no
 * history entry) to encode the API's canonical spec, so equivalent requests
 * share one canonical link — mirroring how the backend itself identifies
 * analyses. The already-fetched result is seeded into the query cache under
 * the canonical key, so the rewrite never refetches.
 */
import { useEffect, useMemo } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router'

import {
  analysisQueryKey,
  refetchFailed,
  useAnalyses,
  useAnalysis,
  useActivities,
  useActivityPresets,
  useMeta,
  usePopulationMetadata,
  useVersionTag,
} from '../api/queries'
import { useServiceWaking } from '../api/serviceStatus'
import type {
  AnalysisRequest,
  AnalysisResponse,
  CompareRequest,
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
  activityName,
  analysisTitle,
  definitionNote,
  resultHeading,
  sharedPopulationPhrase,
} from '../domain/describe'
import { isFullPageError } from '../domain/presentError'
import {
  CUT_OFF,
  InvalidShareLinkError,
  analysisPath,
  decodeSpec,
  encodeSpec,
  explorePath,
  parseSplit,
  type SplitSelection,
} from '../domain/urlSpec'
import {
  choiceDimensions,
  resolveSplit,
  seriesSpec,
} from '../features/population/compareGroups'
import { formatYears } from '../utils/format'
import { CompareView } from '../features/results/CompareView'
import { EstimateView } from '../features/results/EstimateView'
import { ShareButton } from '../features/results/ShareButton'
import { SplitTrendView, TrendView, type SplitSeries } from '../features/results/TrendView'
import { withAnalysisSettings } from '../features/builder/state'
import { needsPartial2020, partial2020Spec } from '../features/results/partial2020'
import { combineYearsLink } from '../features/results/combineYears'
import { splitComparison } from '../features/results/splitComparison'

type ParsedLink =
  | { invalid: string }
  | {
      operation: Operation
      spec: AnalysisRequest
      split: SplitSelection | null
      /** Whether the link's own spec is written otherwise than it runs. */
      rewritten: boolean
      /** Whether it had other settings than the site's (review round 6: say so). */
      resettled: boolean
    }

export function AnalysisPage() {
  const params = useParams()
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()

  const parsed = useMemo((): ParsedLink => {
    const operation = params.operation
    if (!isOperation(operation)) {
      return { invalid: 'It points to a kind of chart this site does not make.' }
    }
    const encoded = searchParams.get('spec')
    if (!encoded) {
      return { invalid: CUT_OFF }
    }
    try {
      const decoded = decodeSpec(encoded, operation)
      const spec = withAnalysisSettings(operation, decoded)
      return {
        operation,
        spec,
        split: parseSplit(searchParams, operation),
        rewritten: encodeSpec(spec) !== encoded,
        // Only the settings: a link whose keys are in another order is
        // rewritten too, but runs as written.
        resettled: encodeSpec(spec) !== encodeSpec(decoded),
      }
    } catch (error) {
      return {
        invalid:
          error instanceof InvalidShareLinkError
            ? error.message
            : 'It is not valid, or no longer supported.',
      }
    }
  }, [params.operation, searchParams])

  // The link shows what runs: one written with other settings is rewritten,
  // and the page says so (review round 6).
  useEffect(() => {
    if ('invalid' in parsed || !parsed.rewritten) return
    navigate(analysisPath(parsed.operation, parsed.spec, parsed.split), {
      replace: true,
      state: { resettled: parsed.resettled } satisfies LinkState,
    })
  }, [parsed, navigate])

  if ('invalid' in parsed) {
    return <InvalidLink message={parsed.invalid} />
  }
  return <AnalysisRunner operation={parsed.operation} spec={parsed.spec} split={parsed.split} />
}

/**
 * What a result page carries in its history entry: whether its link was
 * written with settings the site no longer uses (review round 6).
 */
interface LinkState {
  resettled?: boolean
}

/**
 * A link that opens no chart: a calm card, not an alarm (review round 6),
 * with the two ways on.
 */
function InvalidLink({ message }: { message: string }) {
  return (
    <div className="card notice-page" role="alert">
      <h1 tabIndex={-1}>This link doesn't open a chart</h1>
      <p>{message}</p>
      <p className="notice-page__actions">
        <Link className="btn btn--primary" to="/explore">
          Make a chart
        </Link>
        <Link className="btn" to="/">
          Home
        </Link>
      </p>
    </div>
  )
}

const LOADING_MESSAGES: Record<Operation, string> = {
  estimate: 'Working out the number…',
  trend: 'Working out the trend…',
  compare: 'Comparing the two groups…',
}
const SPLIT_LOADING_MESSAGE = 'Working out a line for each group…'
const UNKNOWN_SPLIT = 'It splits the trend into groups that this data release does not offer.'

/** Before /meta arrives no analysis has been sent, so nothing is "calculating" yet (audit F11). */
const PREPARING_MESSAGE = 'Getting the chart ready…'
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
  const location = useLocation()
  const resettled = (location.state as LinkState | null)?.resettled === true
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
  const seriesSpecs = resolved
    ? resolved.options.map((option) => seriesSpec(spec, resolved, option))
    : []
  const seriesQueries = useAnalyses('trend', seriesSpecs)
  // A trend through 2020 also asks for 2020's partial-year estimate, one per
  // line, once the trend has answered: built from the trend's canonical spec
  // (the same before and after the link is rewritten to it) with the preset's
  // name, so it matches the snapshot's prewarmed estimate for a preset.
  const wantsPartial = operation === 'trend' && needsPartial2020(spec)
  const trendResults =
    split === null
      ? [single.data?.data as TrendResponse | undefined]
      : seriesQueries.map((entry) => entry.data?.data)
  const partialSpecs =
    wantsPartial &&
    meta.data !== undefined &&
    (presets.data !== undefined || presets.isError) &&
    trendResults.every((trend) => trend !== undefined)
      ? trendResults.map((trend) =>
          partial2020Spec(
            (trend as TrendResponse).spec as AnalysisRequest,
            meta.data?.data.years ?? [],
            presets.data?.presets ?? [],
          ),
        )
      : []
  const partialQueries = useAnalyses('estimate', partialSpecs)
  const partialPending =
    wantsPartial &&
    (partialSpecs.length === 0 || partialQueries.some((entry) => entry.isPending))
  const query = useCombinedQuery(single, split === null ? null : seriesQueries, partialPending)
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
  // The survey's own activity names, against which a link's label is read
  // (definitionNote, R14). From the static snapshot, like all metadata.
  const activities = useActivities()
  const surveyNames = useMemo(
    () =>
      activities.data
        ? new Map(activities.data.activities.map((entry) => [entry.code, entry.name]))
        : undefined,
    [activities.data],
  )
  const note = definitionNote(operation, spec, presets.data?.presets, surveyNames)

  const result: AnalysisResponse | undefined = query.data?.data ?? query.series?.[0]
  const activityLabel = useMemo(() => {
    if (result) {
      if (operation === 'compare') return (result as CompareResponse).group_a.activity.label
      return (result as EstimateResponse | TrendResponse).activity.label
    }
    return activityLabelFromSpec(spec, presetLabels)
  }, [result, operation, spec, presetLabels])

  const splitPhrase = resolved ? `by ${resolved.choice.label.toLowerCase()}` : null
  // The subtitle names a comparison's shared filters: they restrict both
  // groups and must be named with the result, not only inside its collapsed
  // panels (review round 3).
  const heading = resultHeading(operation, spec, activityLabel)
  if (resolved !== null) {
    // The groups say who; the population is named only when it narrows them.
    const groups = resolved.options.map((option) => option.label)
    const shared = { ...spec.population } as Record<string, unknown>
    for (const name of choiceDimensions(resolved.choice.by)) delete shared[name]
    const sharedPhrase = sharedPopulationPhrase({ ...spec, population: shared })
    heading.eyebrow = `${heading.eyebrow} and ${resolved.choice.label.toLowerCase()}`
    heading.subtitle = [
      groups.length <= 3 ? groups.join(', ') : `${groups.length} groups`,
      sharedPhrase,
      formatYears(spec.years),
    ]
      .filter((part) => part !== null)
      .join(' · ')
  }

  // A split trend's title names its groups, as its subtitle does.
  const title =
    resolved !== null
      ? [
          activityName(activityLabel),
          heading.subtitle.replaceAll(' · ', ' — '),
          splitPhrase,
        ].join(' — ')
      : analysisTitle(operation, spec, activityLabel)

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
    navigate(`/analysis/${operation}?spec=${canonicalEncoded}`, {
      replace: true,
      state: location.state,
    })
  }, [
    result,
    operation,
    spec,
    split,
    versions,
    navigate,
    query.data,
    queryClient,
    location.state,
  ])

  const adjustHref = explorePath(operation, spec, split)
  const trendPath =
    operation === 'estimate' && meta.data && trendShortcut(spec)
      ? analysisPath(
          'trend',
          withAnalysisSettings('trend', { ...spec, years: meta.data.data.years }),
        )
      : null
  const allYears = meta.data ? formatYears(meta.data.data.years) : ''
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
          {resettled ? (
            <p className="analysis-header__note" data-testid="resettled-note">
              This link was made with older settings. It now uses the site's current ones, so
              its numbers may differ slightly from when it was shared.
            </p>
          ) : null}
        </div>
        <div className="analysis-header__actions">
          <ShareButton />
          <Link className="btn btn--small" to={adjustHref}>
            Change this chart
          </Link>
          {trendPath !== null ? (
            <Link className="btn btn--small analysis-header__trend" to={trendPath}>
              View {allYears} trend
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
        // In the result's own card, at a chart's height, so nothing jumps
        // when the chart arrives (review round 4).
        <section className="card result-card result-card--loading" aria-label="Loading">
          <LoadingPanel
            announce={false}
            message={loadingMessage}
            hint={
              meta.data !== undefined && !waking
                ? 'The first time can take up to a minute; after that it is nearly instant.'
                : undefined
            }
          />
        </section>
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
            filters: option.population,
            result: query.series?.[index] as TrendResponse,
            partial2020: partialQueries[index]?.data?.data,
          }))}
          legendTitle={resolved.choice.label}
          note={note}
          comparison={
            meta.data
              ? splitComparison(spec, resolved, query.series, meta.data.data.years)
              : null
          }
        />
      ) : query.data !== undefined ? (
        <ResultBody
          operation={operation}
          spec={spec}
          result={query.data.data}
          partial2020={partialQueries[0]?.data?.data}
          note={note}
          combine={meta.data ? combineYearsLink(operation, spec, meta.data.data.years) : null}
        />
      ) : null}
      {/* On a phone the next question, every year, follows the answer
          instead of crowding the buttons above it (review round 6). */}
      {trendPath !== null && query.data !== undefined && !query.isError ? (
        <p className="analysis-next">
          <Link className="btn" to={trendPath}>
            See every year, {allYears}
          </Link>
        </p>
      ) : null}
      {/* Persistent live region: reliably announces the start and end of a
          potentially ~10 s computation (content mounted with its text
          already present is often not announced by screen readers). */}
      <p className="visually-hidden" aria-live="polite">
        {metaFailed || query.isError
          ? 'The chart could not be drawn.'
          : meta.data === undefined || query.isPending
            ? announcedLoadingMessage
            : 'Chart ready.'}
      </p>
    </div>
  )
}

/**
 * Whether an estimate offers its full-period trend: under the multi-year
 * weights, or a 2020 estimate under the pandemic weights (the site's
 * weights for 2020), whose trend shows 2020 as its partial-year point.
 * Other pandemic-weight estimates (2019 and 2020 combined, or a diary-date
 * window) have no full-period counterpart.
 */
function trendShortcut(spec: AnalysisRequest): boolean {
  if (spec.weights !== 'pandemic') return true
  const population = spec.population ?? {}
  return (
    spec.years.length === 1 &&
    spec.years[0] === 2020 &&
    population.diary_date_min == null &&
    population.diary_date_max == null
  )
}

function ResultBody({
  operation,
  spec,
  result,
  partial2020,
  note,
  combine,
}: {
  operation: Operation
  spec: AnalysisRequest
  result: AnalysisResponse
  partial2020?: EstimateResponse
  note: string | null
  combine: ReturnType<typeof combineYearsLink>
}) {
  if (operation === 'estimate') {
    return <EstimateView result={result as EstimateResponse} note={note} combine={combine} />
  }
  if (operation === 'trend') {
    return <TrendView result={result as TrendResponse} partial2020={partial2020} note={note} />
  }
  return (
    <CompareView
      result={result as CompareResponse}
      spec={spec as CompareRequest}
      note={note}
      combine={combine}
    />
  )
}

/**
 * One analysis, or a split trend's analyses as one: pending while any is,
 * failed (with the first failure) when any failed, and "Try again" refetches
 * every failed one. `series` holds the split's results, in group order.
 * `partialPending`: a trend's 2020 partial-year estimates are still on their
 * way — the chart waits for them (their failure only leaves 2020 a gap).
 */
function useCombinedQuery(
  single: ReturnType<typeof useAnalysis>,
  many: ReturnType<typeof useAnalyses<'trend'>> | null,
  partialPending: boolean,
) {
  if (many === null) {
    return {
      isPending: single.isPending || (partialPending && !single.isError),
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
    isPending: (!done || partialPending) && failed === undefined,
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
