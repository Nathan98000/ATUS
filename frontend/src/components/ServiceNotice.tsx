import { useEffect, useState } from 'react'

import {
  useChangedAnalyticsVersion,
  useChangedDataVersion,
  useServiceWaking,
} from '../api/serviceStatus'
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
 *   publishes the new snapshot (ADR-013 step 8). While that is so, the
 *   snapshot pointer is read again every minute, so the notice offers the
 *   reload once the new snapshot is published. A release that changes only
 *   the analytics version (the statistical implementation) gets the same
 *   two notices in its own words (review round 2).
 */
export function ServiceNotice() {
  const waking = useServiceWaking()
  const changedVersion = useChangedDataVersion()
  const changedAnalytics = useChangedAnalyticsVersion()
  const reloadHelps = useReloadHelps(changedVersion, changedAnalytics)
  // Only the statistical implementation changed, with the same data.
  const analyticsOnly = changedVersion === null && changedAnalytics !== null

  return (
    <>
      <output className="service-notice-region">
        {waking ? <span className="service-notice">{WAKING_MESSAGE}</span> : null}
      </output>
      <output className="service-notice-region">
        {analyticsOnly && reloadHelps === true ? (
          <span className="service-notice service-notice--reload">
            <span>
              The analysis service now runs a different version of its statistical methods
              (analytics {changedAnalytics}) than the one this page loaded. Reload so that every
              number comes from the same version.
            </span>
            <button
              type="button"
              className="btn btn--small"
              onClick={() => window.location.reload()}
            >
              Reload page
            </button>
          </span>
        ) : analyticsOnly && reloadHelps === false ? (
          <span className="service-notice service-notice--reload">
            <span>
              The analysis service is switching to a different version of its statistical
              methods (analytics {changedAnalytics}): new results already come from it, while
              this page’s examples and descriptions still come from the previous one, and
              reloading the page does not change that yet. The switch usually completes within
              minutes; this notice then offers a reload.
            </span>
          </span>
        ) : changedVersion && reloadHelps === true ? (
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
              completes within minutes; this notice then offers a reload.
            </span>
          </span>
        ) : null}
      </output>
    </>
  )
}

/**
 * While a reload would not help, the snapshot pointer is read again this
 * often. It is a static file (no Worker request, never the API), and it is
 * read only while the page is visible.
 */
const RELOAD_RECHECK_MS = 60_000

/**
 * Whether a reload would load the changed versions (null while unknown or
 * without a change). A "no" is checked again every `RELOAD_RECHECK_MS`: the
 * release switch ends when the deploy job publishes the new snapshot, and a
 * page left open through it should then offer the reload (review round 1).
 */
function useReloadHelps(
  dataVersion: string | null,
  analyticsVersion: string | null,
): boolean | null {
  const [answer, setAnswer] = useState<{ version: string; helps: boolean } | null>(null)
  // One key for the pair, so the effect restarts only when either changes.
  const version =
    dataVersion === null && analyticsVersion === null
      ? null
      : JSON.stringify([dataVersion, analyticsVersion])
  useEffect(() => {
    if (version === null) return
    const target = JSON.parse(version) as [string | null, string | null]
    let current = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const recheckLater = () => {
      timer = setTimeout(() => {
        if (document.visibilityState === 'hidden') recheckLater()
        else check()
      }, RELOAD_RECHECK_MS)
    }
    const check = () => {
      void reloadLoadsVersion({
        dataVersion: target[0],
        analyticsVersion: target[1],
      }).then((helps) => {
        if (!current) return
        setAnswer({ version, helps })
        if (!helps) recheckLater()
      })
    }
    check()
    return () => {
      current = false
      clearTimeout(timer)
    }
  }, [version])
  return answer !== null && answer.version === version ? answer.helps : null
}
