/**
 * What the browser has learned about the analysis service across requests.
 *
 * TanStack Query caches data per request; these two facts span requests, so
 * they live in small module-level stores that components read through
 * `useSyncExternalStore`:
 *
 * - **Waking.** Free hosting sleeps when idle, and the first request after a
 *   quiet period can take about a minute. The UI says so instead of showing an
 *   unexplained spinner or an error (audit F11, F12). A call counts as waking
 *   when a metadata request has been pending for `SLOW_METADATA_MS`, when an
 *   attempt failed in a way that means the host is not up yet, or when the
 *   edge's prewake answer said the origin is waking. Both time limits are
 *   decided by their own timers, never by re-reading the clock when a timer
 *   fires: Firefox reduces and jitters the precision of `Date.now()`
 *   (privacy.reduceTimerPrecision), so a 3 s timer can read 2999 ms elapsed,
 *   and a re-check would then leave a cold start without its notice.
 * - **Data version.** The release the page was loaded with, compared with the
 *   `X-Data-Version` of every later response. A difference means the service
 *   switched releases under an open tab, and the UI offers a reload (F27).
 * - **Analytics version.** The same for the statistical implementation: the
 *   page's `/meta` `analytics_version`, compared with every later
 *   `X-Analytics-Version` and every result's own `analytics_version`. A
 *   release can change it alone, with the same data (review round 2).
 */
import { useSyncExternalStore } from 'react'

/** A metadata request still pending after this long is presented as a cold start. */
export const SLOW_METADATA_MS = 3_000

/** How long a prewake answer of "waking" is trusted without a later API answer. */
export const ORIGIN_WAKING_TTL_MS = 90_000

export type CallKind = 'metadata' | 'analysis'

interface TrackedCall {
  kind: CallKind
  waking: boolean
  /** A metadata call whose `SLOW_METADATA_MS` timer fired while it was pending. */
  slow: boolean
}

export interface CallTracker {
  /** The latest attempt failed because the host is not up yet (or recovered). */
  setWaking(value: boolean): void
  /** The API itself answered, with any status: the service is awake. */
  answered(): void
  /** The call settled (success, final failure, or abort). */
  finish(): void
}

const calls = new Set<TrackedCall>()
const timers = new Set<ReturnType<typeof setTimeout>>()
/** The prewake said "waking", and neither the API nor `ORIGIN_WAKING_TTL_MS` has since. */
let originWaking = false
let originTimer: ReturnType<typeof setTimeout> | null = null
let waking = false
const wakingListeners = new Set<() => void>()

function computeWaking(): boolean {
  for (const call of calls) {
    if (call.waking || call.slow || originWaking) return true
  }
  return false
}

function refreshWaking(): void {
  const next = computeWaking()
  if (next === waking) return
  waking = next
  for (const listener of wakingListeners) listener()
}

/** Runs `expire` after `delayMs`, then refreshes the waking state. */
function schedule(delayMs: number, expire: () => void): ReturnType<typeof setTimeout> {
  const timer = setTimeout(() => {
    timers.delete(timer)
    expire()
    refreshWaking()
  }, delayMs)
  timers.add(timer)
  return timer
}

function cancel(timer: ReturnType<typeof setTimeout> | null): void {
  if (timer === null) return
  clearTimeout(timer)
  timers.delete(timer)
}

function clearOriginWaking(): void {
  originWaking = false
  cancel(originTimer)
  originTimer = null
}

/** Registers one logical API call (all of its attempts) for the waking notice. */
export function trackApiCall(kind: CallKind): CallTracker {
  const call: TrackedCall = { kind, waking: false, slow: false }
  calls.add(call)
  // The timer firing is the fact "pending for SLOW_METADATA_MS" (see above).
  const timer =
    kind === 'metadata'
      ? schedule(SLOW_METADATA_MS, () => {
          call.slow = true
        })
      : null
  refreshWaking()
  return {
    setWaking(value) {
      call.waking = value
      refreshWaking()
    },
    answered() {
      call.waking = false
      clearOriginWaking()
      refreshWaking()
    },
    finish() {
      calls.delete(call)
      cancel(timer)
      refreshWaking()
    },
  }
}

/** Records the edge's prewake answer (`GET /api/v1/wake`). */
export function setOriginState(origin: 'warm' | 'waking'): void {
  clearOriginWaking()
  if (origin === 'waking') {
    originWaking = true
    originTimer = schedule(ORIGIN_WAKING_TTL_MS, () => {
      originWaking = false
      originTimer = null
    })
  }
  refreshWaking()
}

function subscribeWaking(listener: () => void): () => void {
  wakingListeners.add(listener)
  return () => wakingListeners.delete(listener)
}

/** True while a pending API call is (probably) waiting for the service to wake. */
export function useServiceWaking(): boolean {
  return useSyncExternalStore(
    subscribeWaking,
    () => waking,
    () => false,
  )
}

export function isServiceWaking(): boolean {
  return waking
}

// ---------------------------------------------------------------------------
// Data version

let loadedDataVersion: string | null = null
let changedDataVersion: string | null = null
const versionListeners = new Set<() => void>()

function notifyVersion(): void {
  for (const listener of versionListeners) listener()
}

/**
 * The data version the page's metadata came from. `comparable` is false for
 * the legacy `release:runN` tag, which no response header can be compared with.
 */
export function setLoadedDataVersion(version: string, comparable: boolean): void {
  loadedDataVersion = comparable ? version : null
  if (changedDataVersion !== null && changedDataVersion === loadedDataVersion) {
    changedDataVersion = null
  }
  notifyVersion()
}

/** Called with the `X-Data-Version` of every API response (null when absent). */
export function noteServedDataVersion(version: string | null): void {
  if (version === null || version === '' || loadedDataVersion === null) return
  if (version === loadedDataVersion || version === changedDataVersion) return
  changedDataVersion = version
  notifyVersion()
}

function subscribeVersion(listener: () => void): () => void {
  versionListeners.add(listener)
  return () => versionListeners.delete(listener)
}

/** The data version the service now serves, when it differs from the loaded one. */
export function useChangedDataVersion(): string | null {
  return useSyncExternalStore(
    subscribeVersion,
    () => changedDataVersion,
    () => null,
  )
}

// ---------------------------------------------------------------------------
// Analytics version

let loadedAnalyticsVersion: string | null = null
let changedAnalyticsVersion: string | null = null

/** The analytics version of the page's metadata (`/meta` `analytics_version`). */
export function setLoadedAnalyticsVersion(version: string): void {
  loadedAnalyticsVersion = version
  if (changedAnalyticsVersion !== null && changedAnalyticsVersion === loadedAnalyticsVersion) {
    changedAnalyticsVersion = null
  }
  notifyVersion()
}

/**
 * Called with the `X-Analytics-Version` of every API response and the
 * `analytics_version` of every analysis result (null when absent).
 */
export function noteServedAnalyticsVersion(version: string | null): void {
  if (version === null || version === '' || loadedAnalyticsVersion === null) return
  if (version === loadedAnalyticsVersion || version === changedAnalyticsVersion) return
  changedAnalyticsVersion = version
  notifyVersion()
}

/** The analytics version the service now runs, when it differs from the loaded one. */
export function useChangedAnalyticsVersion(): string | null {
  return useSyncExternalStore(
    subscribeVersion,
    () => changedAnalyticsVersion,
    () => null,
  )
}

/** Test isolation: forget every call, timer, and version. */
export function resetServiceStatus(): void {
  for (const timer of timers) clearTimeout(timer)
  timers.clear()
  calls.clear()
  originWaking = false
  originTimer = null
  waking = false
  loadedDataVersion = null
  changedDataVersion = null
  loadedAnalyticsVersion = null
  changedAnalyticsVersion = null
  for (const listener of wakingListeners) listener()
  notifyVersion()
}
