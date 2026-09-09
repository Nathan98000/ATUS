/**
 * Server state, managed with TanStack Query.
 *
 * Metadata (meta, population metadata, activity lexicon, presets) is fetched
 * once and cached for the session. Analysis results are cached under a key
 * that includes the canonical spec AND the backend's analytics/data versions,
 * so a result computed under one data release or statistical implementation
 * is never silently presented as coming from another (the server's own cache
 * uses the same versioning rule).
 */
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, NetworkError, type ApiResult } from './client'
import {
  fetchActivities,
  fetchActivityDetail,
  fetchActivityPresets,
  fetchMeta,
  fetchPopulationMetadata,
  runAnalysis,
} from './endpoints'
import type { MetaResponse, Operation, OperationContract } from './types'
import { stableStringify } from '../utils/stableStringify'

const METADATA_STALE_MS = 5 * 60 * 1000 // matches the API's Cache-Control max-age

/** Retry infrastructure failures a couple of times; never retry analytical errors. */
function retryInfrastructureOnly(failureCount: number, error: unknown): boolean {
  if (failureCount >= 2) return false
  if (error instanceof NetworkError) return true
  if (error instanceof ApiError) return error.status === 503
  return false
}

export function useMeta() {
  return useQuery({
    queryKey: ['meta'],
    queryFn: ({ signal }) => fetchMeta(signal),
    staleTime: METADATA_STALE_MS,
    retry: retryInfrastructureOnly,
  })
}

export function usePopulationMetadata() {
  return useQuery({
    queryKey: ['population-metadata'],
    queryFn: ({ signal }) => fetchPopulationMetadata(signal),
    staleTime: Infinity,
    retry: retryInfrastructureOnly,
  })
}

export function useActivities() {
  return useQuery({
    queryKey: ['activities'],
    queryFn: ({ signal }) => fetchActivities(signal),
    staleTime: Infinity,
    retry: retryInfrastructureOnly,
  })
}

export function useActivityPresets() {
  return useQuery({
    queryKey: ['activity-presets'],
    queryFn: ({ signal }) => fetchActivityPresets(signal),
    staleTime: Infinity,
    retry: retryInfrastructureOnly,
  })
}

export function useActivityDetail(code: string | null) {
  return useQuery({
    queryKey: ['activity-detail', code],
    queryFn: ({ signal }) => fetchActivityDetail(code as string, signal),
    enabled: code !== null,
    staleTime: Infinity,
    retry: retryInfrastructureOnly,
    placeholderData: keepPreviousData,
  })
}

/** analytics_version + data release + ingestion run — the same triple the server keys its cache on. */
export function versionTag(meta: MetaResponse): string {
  return `${meta.analytics_version}:${meta.data.release}:run${meta.data.ingestion_run_id ?? 0}`
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
 * it. The query key carries the version tag from /meta, so a data reload or
 * analytics change on the server invalidates the frontend cache by
 * construction. Stale responses can never overwrite newer requests: a
 * different spec is a different key, and TanStack Query aborts the
 * superseded request via its AbortSignal.
 */
export function useAnalysis<Op extends Operation>(
  operation: Op,
  spec: OperationContract[Op]['request'] | null,
) {
  const meta = useMeta()
  const versions = meta.data ? versionTag(meta.data) : null

  return useQuery<ApiResult<OperationContract[Op]['response']>, Error>({
    queryKey: analysisQueryKey(operation, spec, versions ?? 'unknown'),
    queryFn: ({ signal }) =>
      runAnalysis(operation, spec as OperationContract[Op]['request'], signal),
    enabled: spec !== null && versions !== null,
    // Results are immutable for a given (spec, versions) key; the version tag
    // in the key does the invalidating.
    staleTime: Infinity,
    gcTime: 30 * 60 * 1000,
    retry: false, // analyses can be expensive — retry only on explicit user action
  })
}

/** Imperative refetch helper for error-state "Try again" buttons. */
export function useInvalidateAnalysis() {
  const queryClient = useQueryClient()
  return () => queryClient.invalidateQueries({ queryKey: ['analysis'] })
}
