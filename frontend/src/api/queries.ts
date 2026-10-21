/**
 * Server state, managed with TanStack Query.
 *
 * Metadata (meta, population metadata, activity lexicon, presets) is fetched
 * once per page and never refetched in the background: not on window focus,
 * not on a timer (audit F62 — a focus refetch could wake a sleeping host for
 * nothing). A data release change is noticed from the `X-Data-Version` of
 * later responses instead, and the page offers a reload (`serviceStatus.ts`).
 *
 * Analysis results are cached under a key that includes the canonical spec
 * AND the analytics version plus the data version the page was loaded with,
 * so a result computed under one data release or statistical implementation
 * is never silently presented as coming from another.
 *
 * Retries are not TanStack Query's: `endpoints.ts` runs every API call through
 * a time-budgeted retry loop that understands cold starts and `Retry-After`.
 *
 * Every query runs with `networkMode: 'always'`. TanStack Query's default
 * ('online') pauses a query that starts while the browser is offline before
 * its function runs: the page then showed "Calculating…" for as long as the
 * browser stayed offline, with no request and no message. Running the
 * function lets the request fail at once, and `resilience.ts` presents being
 * offline as final ("check your internet connection"); a query that failed
 * BECAUSE the browser was offline is fetched again when it comes back online
 * (`refetchOnReconnect`, which networkMode 'always' turns off unless it is
 * stated). Any other failure is an answer that coming back online does not
 * change — an analysis time-out, a paused quota, a rate limit whose
 * Retry-After still runs, a 422, an incompatible API — and is never re-sent
 * by a reconnect: the visitor decides (review round 2). Loaded data never
 * goes stale (`staleTime: Infinity`), so a reconnect refetches nothing that
 * has data.
 */
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'

import type { ApiResult } from './client'
import { isOfflineFailure } from './resilience'
import {
  fetchActivities,
  fetchActivityDetail,
  fetchActivityPresets,
  fetchMeta,
  fetchPopulationMetadata,
  runAnalysis,
  type LoadedMeta,
} from './endpoints'
import type { MetaResponse, Operation, OperationContract } from './types'
import { stableStringify } from '../utils/stableStringify'

/**
 * Coming back online refetches a query only when its last attempt failed
 * because the browser was offline (or it has neither data nor an error).
 * A final answer — or a rate limit still counting down — is not re-sent.
 */
function refetchOnReconnect(query: { state: { error: unknown } }): boolean {
  return query.state.error === null || isOfflineFailure(query.state.error)
}

/**
 * Metadata describes one deployed release: fetch it once per page. Loaded
 * data never goes stale, so nothing refetches it; only a query that failed
 * because the browser was offline is retried when it comes back online.
 */
const METADATA_QUERY = {
  staleTime: Infinity,
  retry: false,
  refetchOnWindowFocus: false,
  networkMode: 'always',
  refetchOnReconnect,
} as const

const META_QUERY_KEY = ['meta'] as const

/** analytics_version + data version — the same pair the server keys its cache on. */
export function versionTag(loaded: LoadedMeta): string {
  return `${loaded.meta.analytics_version}:${loaded.dataVersion}`
}

function selectMeta(loaded: LoadedMeta): MetaResponse {
  return loaded.meta
}

function fetchLoadedMeta({ signal }: { signal: AbortSignal }): Promise<LoadedMeta> {
  return fetchMeta(signal)
}

export function useMeta() {
  return useQuery({
    queryKey: META_QUERY_KEY,
    queryFn: fetchLoadedMeta,
    select: selectMeta,
    ...METADATA_QUERY,
  })
}

/** The version tag of the loaded metadata, or null while it is not loaded. */
export function useVersionTag(): string | null {
  const query = useQuery({
    queryKey: META_QUERY_KEY,
    queryFn: fetchLoadedMeta,
    select: versionTag,
    ...METADATA_QUERY,
  })
  return query.data ?? null
}

function selectDataVersion(loaded: LoadedMeta): string {
  return loaded.dataVersion
}

/** The data version the page's metadata came from (`LoadedMeta.dataVersion`), or null. */
export function useLoadedDataVersion(): string | null {
  const query = useQuery({
    queryKey: META_QUERY_KEY,
    queryFn: fetchLoadedMeta,
    select: selectDataVersion,
    ...METADATA_QUERY,
  })
  return query.data ?? null
}

export function usePopulationMetadata() {
  return useQuery({
    queryKey: ['population-metadata'],
    queryFn: ({ signal }) => fetchPopulationMetadata(signal),
    ...METADATA_QUERY,
  })
}

export function useActivities() {
  return useQuery({
    queryKey: ['activities'],
    queryFn: ({ signal }) => fetchActivities(signal),
    ...METADATA_QUERY,
  })
}

export function useActivityPresets() {
  return useQuery({
    queryKey: ['activity-presets'],
    queryFn: ({ signal }) => fetchActivityPresets(signal),
    ...METADATA_QUERY,
  })
}

export function useActivityDetail(code: string | null) {
  return useQuery({
    queryKey: ['activity-detail', code],
    queryFn: ({ signal }) => fetchActivityDetail(code as string, signal),
    enabled: code !== null,
    placeholderData: keepPreviousData,
    ...METADATA_QUERY,
  })
}

export function analysisQueryKey(
  operation: Operation,
  spec: unknown,
  versions: string,
): readonly unknown[] {
  return ['analysis', operation, versions, stableStringify(spec)]
}

/**
 * Runs one analysis. `spec` may be null while the caller is still assembling
 * it. The query key carries the version tag of the loaded metadata, so
 * results of different releases never share a cache entry. Stale responses
 * can never overwrite newer requests: a different spec is a different key,
 * and TanStack Query aborts the superseded request via its AbortSignal.
 */
export function useAnalysis<Op extends Operation>(
  operation: Op,
  spec: OperationContract[Op]['request'] | null,
) {
  const versions = useVersionTag()

  return useQuery<ApiResult<OperationContract[Op]['response']>, Error>({
    queryKey: analysisQueryKey(operation, spec, versions ?? 'unknown'),
    queryFn: ({ signal }) =>
      runAnalysis(operation, spec as OperationContract[Op]['request'], signal),
    enabled: spec !== null && versions !== null,
    // Results are immutable for a given (spec, versions) key; the version tag
    // in the key does the invalidating.
    staleTime: Infinity,
    gcTime: 30 * 60 * 1000,
    // Automatic retries happen inside runAnalysis (cold start, one transient
    // 503); anything else waits for the visitor's "Try again".
    retry: false,
    refetchOnWindowFocus: false,
    networkMode: 'always',
    refetchOnReconnect,
  })
}

/** Imperative refetch helper for error-state "Try again" buttons. */
export function useInvalidateAnalysis() {
  const queryClient = useQueryClient()
  return () => queryClient.invalidateQueries({ queryKey: ['analysis'] })
}

/** "Try again" for a page that depends on several queries: refetch every one that failed (F63). */
export function refetchFailed(
  ...queries: readonly { isError: boolean; refetch: () => Promise<unknown> }[]
): void {
  for (const query of queries) {
    if (query.isError) void query.refetch()
  }
}
