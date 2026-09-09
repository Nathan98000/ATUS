/**
 * The analysis builder. Builder edits are local state; pressing Analyze
 * commits the analysis to a shareable /analysis URL (browser history gets
 * one entry per run, not one per keystroke). Opening /explore?op=…&spec=…
 * prefills the builder from an existing analysis.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router'

import { useMeta, usePopulationMetadata } from '../api/queries'
import { isOperation, type Operation } from '../api/types'
import { ErrorPanel } from '../components/ErrorPanel'
import { LoadingPanel } from '../components/LoadingPanel'
import { analysisPath, decodeSpec, explorePath } from '../domain/urlSpec'
import { ActivityPicker } from '../features/activities/ActivityPicker'
import {
  AdvancedOptions,
  MeasureSelector,
  YearSelector,
} from '../features/builder/BuilderControls'
import {
  defaultBuilderState,
  fromRequest,
  toRequest,
  validateBuilder,
  type BuilderState,
} from '../features/builder/state'
import { PopulationFields } from '../features/population/PopulationFields'

const OPERATION_LABELS: { value: Operation; label: string; hint: string }[] = [
  { value: 'estimate', label: 'Single estimate', hint: 'One number for one period' },
  { value: 'trend', label: 'Trend over time', hint: 'One estimate per year' },
  {
    value: 'compare',
    label: 'Compare two groups',
    hint: 'Two populations and their difference',
  },
]

export function ExplorePage() {
  const meta = useMeta()
  const populationMetadata = usePopulationMetadata()

  useEffect(() => {
    document.title = 'Explore · ATUS Explorer'
    return () => {
      document.title = 'ATUS Explorer'
    }
  }, [])

  if (meta.isPending || populationMetadata.isPending) {
    return (
      <LoadingPanel
        message="Loading the analysis builder…"
        hint="Fetching what the analytical service supports."
      />
    )
  }
  if (meta.isError) {
    return <ErrorPanel error={meta.error} onRetry={() => meta.refetch()} />
  }
  if (populationMetadata.isError) {
    return (
      <ErrorPanel
        error={populationMetadata.error}
        onRetry={() => populationMetadata.refetch()}
      />
    )
  }

  return (
    <Builder
      metaYears={meta.data.data.years}
      meta={meta.data}
      populationMetadata={populationMetadata.data}
    />
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

  const [state, setState] = useState<BuilderState>(() => {
    const operationParam = searchParams.get('op')
    const specParam = searchParams.get('spec')
    if (isOperation(operationParam ?? undefined) && specParam) {
      try {
        const operation = operationParam as Operation
        return fromRequest(operation, decodeSpec(specParam, operation), metaYears)
      } catch {
        // Fall through to the default analysis on a malformed prefill link.
      }
    }
    return defaultBuilderState(metaYears)
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
    // Write the configuration into the /explore history entry first (replace),
    // then push the result — so browser Back returns to a prefilled builder
    // instead of a reset one.
    navigate(explorePath(state.operation, request), { replace: true })
    navigate(analysisPath(state.operation, request))
  }

  const dimensions = populationMetadata.dimensions

  return (
    <div className="explore-page">
      <h1>Build an analysis</h1>
      <p className="field-hint" style={{ marginTop: '-0.3rem' }}>
        Doing what · for whom · when · measured how — then Analyze.
      </p>

      <div
        className="segmented"
        role="radiogroup"
        aria-label="Analysis type"
        style={{ marginBottom: '1.1rem' }}
      >
        {OPERATION_LABELS.map((option) => (
          <label key={option.value} title={option.hint}>
            <input
              type="radio"
              name="operation"
              checked={state.operation === option.value}
              onChange={() => update({ operation: option.value })}
            />
            <span>{option.label}</span>
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
          <b>The analysis is not ready to run:</b> {Object.values(validation.errors).join(' ')}
        </div>
      ) : null}

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
        </section>

        <section className="card builder-section" aria-labelledby="section-population">
          <h2 id="section-population">
            <span className="builder-step" aria-hidden="true">
              2
            </span>
            For whom?
          </h2>
          {state.operation === 'compare' ? (
            <>
              <p className="field-hint">
                Define the two groups to compare. Filters set under “Shared filters” apply to
                both groups.
              </p>
              <div className="group-editors">
                {(
                  [
                    {
                      key: 'A',
                      label: state.labelA,
                      population: state.groupA,
                      ageError: shownErrors.ageA,
                    },
                    {
                      key: 'B',
                      label: state.labelB,
                      population: state.groupB,
                      ageError: shownErrors.ageB,
                    },
                  ] as const
                ).map((group) => (
                  <fieldset
                    key={group.key}
                    className="group-editor"
                    style={{
                      borderTopColor: group.key === 'A' ? 'var(--group-a)' : 'var(--group-b)',
                    }}
                  >
                    <legend>Group {group.key}</legend>
                    <div className="field" style={{ marginBottom: '0.6rem' }}>
                      <label htmlFor={`group-label-${group.key}`}>Name</label>
                      <input
                        id={`group-label-${group.key}`}
                        type="text"
                        maxLength={80}
                        value={group.label}
                        onChange={(event) =>
                          update(
                            group.key === 'A'
                              ? { labelA: event.target.value }
                              : { labelB: event.target.value },
                          )
                        }
                      />
                    </div>
                    <PopulationFields
                      dimensions={dimensions}
                      value={group.population}
                      onChange={(population) =>
                        update(
                          group.key === 'A' ? { groupA: population } : { groupB: population },
                        )
                      }
                      ageError={group.ageError}
                    />
                  </fieldset>
                ))}
              </div>
              <details className="more-filters" style={{ marginTop: '0.8rem' }}>
                <summary>Shared filters (apply to both groups)</summary>
                <div style={{ marginTop: '0.6rem' }}>
                  <PopulationFields
                    dimensions={dimensions}
                    value={state.population}
                    onChange={(population) => update({ population })}
                    ageError={shownErrors.age}
                  />
                </div>
              </details>
            </>
          ) : (
            <PopulationFields
              dimensions={dimensions}
              value={state.population}
              onChange={(population) => update({ population })}
              ageError={shownErrors.age}
            />
          )}
          <p className="field-hint" style={{ marginTop: '0.6rem' }}>
            {populationMetadata.missing_data_rule}
          </p>
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

        <section className="card builder-section" aria-labelledby="section-measure">
          <h2 id="section-measure">
            <span className="builder-step" aria-hidden="true">
              4
            </span>
            Measured how?
          </h2>
          <MeasureSelector
            measures={meta.capabilities.measures}
            value={state.measure}
            onChange={(measure) => update({ measure })}
          />
        </section>

        <AdvancedOptions
          state={state}
          weightSchemes={meta.capabilities.weight_schemes}
          varianceMethods={meta.capabilities.variance_methods}
          onChange={update}
        />

        <div className="builder-actions">
          <button type="button" className="btn btn--primary" onClick={submit}>
            Analyze
          </button>
          <p className="field-hint" style={{ margin: 0 }}>
            Running an analysis creates a shareable link.
          </p>
        </div>
      </div>
    </div>
  )
}
