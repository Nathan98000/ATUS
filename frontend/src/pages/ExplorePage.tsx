/**
 * The analysis builder. Builder edits are local state; pressing Analyze
 * commits the analysis to a shareable /analysis URL (browser history gets
 * one entry per run, not one per keystroke). Opening /explore?op=…&spec=…
 * prefills the builder from an existing analysis.
 */
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'
import { useNavigate, useSearchParams } from 'react-router'

import { prewake } from '../api/prewake'
import {
  refetchFailed,
  useActivities,
  useActivityPresets,
  useMeta,
  usePopulationMetadata,
} from '../api/queries'
import { isOperation, type Operation } from '../api/types'
import { ErrorPanel } from '../components/ErrorPanel'
import { LoadingPanel } from '../components/LoadingPanel'
import { AnalysisTypeIcon } from '../components/AnalysisTypeIcon'
import { useRetryFocus } from '../components/useRetryFocus'
import { isFullPageError } from '../domain/presentError'
import { analysisPath, decodeSpec, explorePath, parseSplit } from '../domain/urlSpec'
import { ActivityPicker } from '../features/activities/ActivityPicker'
import { MeasureSelector, YearSelector } from '../features/builder/BuilderControls'
import { BuilderSummary } from '../features/builder/BuilderSummary'
import {
  defaultBuilderState,
  fromRequest,
  toRequest,
  validateBuilder,
  type BuilderState,
} from '../features/builder/state'
import { CompareGroupsPicker } from '../features/population/CompareGroupsPicker'
import { PopulationFields } from '../features/population/PopulationFields'
import { SplitPicker } from '../features/population/SplitPicker'
import {
  choiceDimensions,
  choiceLabels,
  compareChoices,
  deriveCompareBy,
  resolveSplit,
} from '../features/population/compareGroups'

/** Change over time first: the builder opens on a trend (review round 4). */
const OPERATION_LABELS: {
  value: Operation
  label: string
  hint: string
  icon: 'number' | 'line' | 'bars'
}[] = [
  { value: 'trend', label: 'Change over time', hint: 'A line across the years', icon: 'line' },
  { value: 'estimate', label: 'One number', hint: 'A single figure', icon: 'number' },
  {
    value: 'compare',
    label: 'Compare two groups',
    hint: 'Two bars side by side',
    icon: 'bars',
  },
]

/** Shown when /explore?op=…&spec=… cannot be read (review round 2). */
const UNUSABLE_PREFILL_NOTE =
  'The chart in this link could not be opened here, so the builder starts from the default chart instead.'

export function ExplorePage() {
  const meta = useMeta()
  const populationMetadata = usePopulationMetadata()
  // The builder's activity picker needs these as well: request them now, in
  // parallel with /meta, instead of after it (audit I14).
  useActivities()
  useActivityPresets()
  const retry = useRetryFocus(meta.isFetching || populationMetadata.isFetching)

  useEffect(() => {
    document.title = 'Explore · ATUS Explorer'
    // Entering Explore signals intent to run a new analysis: let a sleeping
    // API host start waking while the visitor builds the specification.
    void prewake()
    return () => {
      document.title = 'ATUS Explorer'
    }
  }, [])

  // Page-level states only while something essential is missing. Once the
  // builder has its data, a failed background refetch never unmounts it and
  // loses the visitor's edits (audit F26).
  let content: ReactNode
  if (meta.data === undefined || populationMetadata.data === undefined) {
    const failure =
      meta.data === undefined && meta.isError
        ? meta.error
        : populationMetadata.data === undefined && populationMetadata.isError
          ? populationMetadata.error
          : null
    // "Try again" refetches every failed query, not just the one shown (F63).
    const onRetry = () => retry(() => refetchFailed(meta, populationMetadata))
    // A paused quota or an incompatible API is a full page with its own heading.
    if (failure !== null && isFullPageError(failure)) {
      return <ErrorPanel error={failure} onRetry={onRetry} />
    }
    content =
      failure !== null ? (
        <ErrorPanel error={failure} onRetry={onRetry} />
      ) : (
        <LoadingPanel message="Loading the chart builder…" />
      )
  } else {
    content = (
      <Builder
        metaYears={meta.data.data.years}
        meta={meta.data}
        populationMetadata={populationMetadata.data}
      />
    )
  }

  // The page heading stays mounted in every state — loading, error, builder —
  // so "Try again" can keep keyboard focus on it while the retry runs and
  // after one that fails again (review round 2; useRetryFocus).
  return (
    <div className="explore-page">
      <h1 tabIndex={-1}>Make a chart</h1>
      {content}
    </div>
  )
}

function Builder({
  metaYears,
  meta,
  populationMetadata,
}: {
  metaYears: number[]
  meta: NonNullable<ReturnType<typeof useMeta>['data']>
  populationMetadata: NonNullable<ReturnType<typeof usePopulationMetadata>['data']>
}) {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const typeId = useId()

  const [state, setState] = useState<BuilderState>(() => {
    const operationParam = searchParams.get('op')
    const specParam = searchParams.get('spec')
    if (!specParam) return defaultBuilderState(metaYears)
    if (isOperation(operationParam ?? undefined)) {
      try {
        const operation = operationParam as Operation
        const prefilled = fromRequest(operation, decodeSpec(specParam, operation), metaYears)
        const split = parseSplit(searchParams, operation)
        if (split !== null) {
          // A split this release does not offer falls into the catch below.
          const resolved = resolveSplit(populationMetadata.dimensions, split)
          if (resolved === null) throw new Error('unknown split')
          return {
            ...prefilled,
            splitBy: resolved.choice.by,
            splitGroups: resolved.options.map((option) => option.key),
          }
        }
        if (operation !== 'compare') return prefilled
        // A comparison opens as a simple "compare by" when its groups are
        // exactly one, and its shared filters do not touch that dimension.
        const compareBy = deriveCompareBy(
          compareChoices(populationMetadata.dimensions),
          prefilled,
        )
        const touched = Object.keys(prefilled.population).some(
          (name) =>
            (prefilled.population as Record<string, unknown>)[name] != null &&
            (compareBy === 'age'
              ? name === 'age_min' || name === 'age_max'
              : name === compareBy),
        )
        if (touched) return { ...prefilled, compareBy: 'custom' }
        const labels = choiceLabels(
          compareChoices(populationMetadata.dimensions),
          compareBy,
          prefilled,
        )
        return { ...prefilled, ...labels, compareBy }
      } catch {
        // A malformed prefill link: the default analysis, with a note below.
      }
    }
    // Never silently: the visitor followed a link to a particular analysis.
    return { ...defaultBuilderState(metaYears), prefillNote: UNUSABLE_PREFILL_NOTE }
  })
  const [failedSubmits, setFailedSubmits] = useState(0)
  const attempted = failedSubmits > 0
  const errorSummaryRef = useRef<HTMLDivElement>(null)

  // Focus the validation summary AFTER it renders (it does not exist at the
  // moment a failed submit is registered).
  useEffect(() => {
    if (failedSubmits > 0) errorSummaryRef.current?.focus()
  }, [failedSubmits])

  const update = (partial: Partial<BuilderState>) =>
    setState((current) => ({ ...current, ...partial }))

  const validation = useMemo(() => validateBuilder(state, metaYears), [state, metaYears])
  const shownErrors = attempted ? validation.errors : {}

  const submit = () => {
    if (!validation.valid) {
      setFailedSubmits((count) => count + 1)
      return
    }
    const request = toRequest(state, metaYears)
    const split =
      state.operation === 'trend' && state.splitBy !== null
        ? { by: state.splitBy, groups: state.splitGroups }
        : null
    // Write the configuration into the /explore history entry first (replace),
    // then push the result — so browser Back returns to a prefilled builder
    // instead of a reset one.
    navigate(explorePath(state.operation, request, split), { replace: true })
    navigate(analysisPath(state.operation, request, split))
  }

  const dimensions = populationMetadata.dimensions

  const chooseOperation = (operation: Operation) => {
    if (operation === 'compare' && state.compareBy !== 'custom') {
      // A filter carried over from an estimate on the compared dimension
      // ("Women") would contradict the groups ("Men vs Women").
      const compared = state.compareBy === 'age' ? ['age_min', 'age_max'] : [state.compareBy]
      const population = { ...state.population }
      for (const name of compared) delete (population as Record<string, unknown>)[name]
      update({ operation, population })
      return
    }
    update({ operation })
  }

  return (
    <>
      <div className="analysis-types" role="radiogroup" aria-label="Analysis type">
        {OPERATION_LABELS.map((option) => (
          <label key={option.value} className="analysis-type">
            <input
              type="radio"
              name="operation"
              aria-label={option.label}
              aria-describedby={`${typeId}-${option.value}`}
              checked={state.operation === option.value}
              onChange={() => chooseOperation(option.value)}
            />
            <span className="analysis-type__card">
              <AnalysisTypeIcon kind={option.icon} />
              <span className="analysis-type__text">
                <span className="analysis-type__label">{option.label}</span>
                <span className="analysis-type__hint" id={`${typeId}-${option.value}`}>
                  {option.hint}
                </span>
              </span>
            </span>
          </label>
        ))}
      </div>

      {state.prefillNote ? (
        <p className="info-note" style={{ marginBottom: '1rem' }}>
          {state.prefillNote}
        </p>
      ) : null}
      {attempted && !validation.valid ? (
        <div
          ref={errorSummaryRef}
          tabIndex={-1}
          className="error-banner"
          role="alert"
          style={{ marginBottom: '1rem', padding: '0.7rem 1rem' }}
        >
          <b>Before showing the chart:</b> {Object.values(validation.errors).join(' ')}
        </div>
      ) : null}

      <div className="builder-layout">
        <div className="builder-sections">
          <section className="card builder-section" aria-labelledby="section-activity">
            <h2 id="section-activity">
              <span className="builder-step" aria-hidden="true">
                1
              </span>
              Doing what?
            </h2>
            <ActivityPicker
              value={state.activity}
              onChange={(activity) => update({ activity })}
              error={shownErrors.activity}
            />
            <MeasureSelector
              measures={meta.capabilities.measures}
              value={state.measure}
              onChange={(measure) => update({ measure })}
            />
          </section>

          <section className="card builder-section" aria-labelledby="section-population">
            <h2 id="section-population">
              <span className="builder-step" aria-hidden="true">
                2
              </span>
              {state.operation === 'compare' ? 'Which two groups?' : 'For whom?'}
            </h2>
            {state.operation === 'compare' ? (
              <CompareGroupsPicker
                state={state}
                dimensions={dimensions}
                errors={shownErrors}
                onChange={update}
              />
            ) : (
              <>
                {state.operation === 'trend' ? (
                  <SplitPicker
                    state={state}
                    dimensions={dimensions}
                    error={shownErrors.groups}
                    onChange={update}
                  />
                ) : null}
                <PopulationFields
                  dimensions={
                    state.operation === 'trend' && state.splitBy !== null
                      ? dimensions.filter(
                          (dimension) =>
                            !choiceDimensions(state.splitBy ?? 'custom').includes(
                              dimension.name,
                            ),
                        )
                      : dimensions
                  }
                  value={state.population}
                  onChange={(population) => update({ population })}
                  ageError={shownErrors.age}
                />
              </>
            )}
          </section>

          <section className="card builder-section" aria-labelledby="section-years">
            <h2 id="section-years">
              <span className="builder-step" aria-hidden="true">
                3
              </span>
              When?
            </h2>
            <YearSelector
              state={state}
              availableYears={metaYears}
              error={shownErrors.years}
              onChange={update}
            />
          </section>
        </div>
        <BuilderSummary
          state={state}
          availableYears={metaYears}
          dimensions={dimensions}
          onAnalyze={submit}
        />
      </div>
    </>
  )
}
