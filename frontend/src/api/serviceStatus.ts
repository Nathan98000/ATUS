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
 *   edge's prewake answer said the origin is waking.
 * - **Data version.** The release the page was loaded with, compared with the
 *   `X-Data-Version` of every later response. A difference means the service
 *   switched releases under an open tab, and the UI offers a reload (F27).
 */
import { useSyncExternalStore } from 'react'

/** A metadata request still pending after this long is presented as a cold start. */
export const SLOW_METADATA_MS = 3_000

/** How long a prewake answer of "waking" is trusted without a later API answer. */
export const ORIGIN_WAKING_TTL_MS = 90_000

export type CallKind = 'metadata' | 'analysis'

interface TrackedCall {
  kind: CallKind
  startedAt: number
  waking: boolean
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
let originWakingUntil = 0
let waking = false
const wakingListeners = new Set<() => void>()

function computeWaking(now: number): boolean {
  for (const call of calls) {
    if (call.waking || now < originWakingUntil) return true
    if (call.kind === 'metadata' && now - call.startedAt >= SLOW_METADATA_MS) return true
  }
  return false
}

function refreshWaking(): void {
  const next = computeWaking(Date.now())
  if (next === waking) return
  waking = next
  for (const listener of wakingListeners) listener()
}

function scheduleRefresh(delayMs: number): ReturnType<typeof setTimeout> {
  const timer = setTimeout(() => {
    timers.delete(timer)
    refreshWaking()
  }, delayMs)
  timers.add(timer)
  return timer
}

/** Registers one logical API call (all of its attempts) for the waking notice. */
export function trackApiCall(kind: CallKind): CallTracker {
  const call: TrackedCall = { kind, startedAt: Date.now(), waking: false }
  calls.add(call)
  const timer = kind === 'metadata' ? scheduleRefresh(SLOW_METADATA_MS) : null
  refreshWaking()
  return {
    setWaking(value) {
      call.waking = value
      refreshWaking()
    },
    answered() {
      call.waking = false
      originWakingUntil = 0
      refreshWaking()
    },
    finish() {
      calls.delete(call)
      if (timer !== null) {
        clearTimeout(timer)
        timers.delete(timer)
      }
      refreshWaking()
    },
  }
}

/** Records the edge's prewake answer (`GET /api/v1/wake`). */
export function setOriginState(origin: 'warm' | 'waking'): void {
  if (origin === 'waking') {
    originWakingUntil = Date.now() + ORIGIN_WAKING_TTL_MS
    scheduleRefresh(ORIGIN_WAKING_TTL_MS + 1)
  } else {
    originWakingUntil = 0
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

/** Test isolation: forget every call, timer, and version. */
export function resetServiceStatus(): void {
  for (const timer of timers) clearTimeout(timer)
  timers.clear()
  calls.clear()
  originWakingUntil = 0
  waking = false
  loadedDataVersion = null
  changedDataVersion = null
  for (const listener of wakingListeners) listener()
  notifyVersion()
}
