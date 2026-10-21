import { useEffect, useState } from 'react'

import { useChangedDataVersion, useServiceWaking } from '../api/serviceStatus'
import { reloadLoadsVersion } from '../api/snapshot'

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
 * - Release change: shown when a response came from a different data release
 *   than the one this page loaded (F27). It offers a reload only when a
 *   reload would load that release: during a release switch the API serves
 *   the new release while the deployed snapshot, which every page load reads,
 *   is still the old one, and reloading cannot help until the deploy job
 *   publishes the new snapshot (ADR-013 step 8).
 */
export function ServiceNotice() {
  const waking = useServiceWaking()
  const changedVersion = useChangedDataVersion()
  const reloadHelps = useReloadHelps(changedVersion)

  return (
    <>
      <output className="service-notice-region">
        {waking ? <span className="service-notice">{WAKING_MESSAGE}</span> : null}
      </output>
      <output className="service-notice-region">
        {changedVersion && reloadHelps === true ? (
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
        ) : changedVersion && reloadHelps === false ? (
          <span className="service-notice service-notice--reload">
            <span>
              The analysis service is switching to a different data release: new results already
              come from it, while this page’s examples and descriptions still come from the
              previous one, and reloading the page does not change that yet. The switch usually
              completes within minutes.
            </span>
          </span>
        ) : null}
      </output>
    </>
  )
}

/** Whether a reload would load `version` (null while unknown or without a change). */
function useReloadHelps(version: string | null): boolean | null {
  const [answer, setAnswer] = useState<{ version: string; helps: boolean } | null>(null)
  useEffect(() => {
    if (version === null) return
    let current = true
    void reloadLoadsVersion(version).then((helps) => {
      if (current) setAnswer({ version, helps })
    })
    return () => {
      current = false
    }
  }, [version])
  return answer !== null && answer.version === version ? answer.helps : null
}
