import { useChangedDataVersion, useServiceWaking } from '../api/serviceStatus'

export const WAKING_MESSAGE =
  'Waking up the analysis service — free hosting sleeps when idle; this can take up to a minute.'

/**
 * Site-wide service notices. Both live in persistent polite live regions:
 * the containers are always rendered (so screen readers announce text added
 * later), nothing ever moves focus, and there is no progress bar — the
 * service reports no progress, so none is invented.
 *
 * - Waking: shown while an API call waits on a sleeping free-tier host
 *   (`serviceStatus.ts` decides when; audit F11).
 * - Reload: shown when a response came from a different data release than
 *   the one this page loaded, e.g. after a release switch (F27).
 */
export function ServiceNotice() {
  const waking = useServiceWaking()
  const changedVersion = useChangedDataVersion()

  return (
    <>
      <output className="service-notice-region">
        {waking ? <span className="service-notice">{WAKING_MESSAGE}</span> : null}
      </output>
      <output className="service-notice-region">
        {changedVersion ? (
          <span className="service-notice service-notice--reload">
            <span>
              The analysis service now serves a different data release than the one this page
              loaded. Reload so that every number comes from the same release.
            </span>
            <button
              type="button"
              className="btn btn--small"
              onClick={() => window.location.reload()}
            >
              Reload page
            </button>
          </span>
        ) : null}
      </output>
    </>
  )
}
