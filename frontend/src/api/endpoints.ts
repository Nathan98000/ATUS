/** Typed functions for every Phase 3 endpoint the frontend uses. */
import { apiRequest, type ApiResult } from './client'
import type {
  ActivityDetailResponse,
  ActivityListResponse,
  ActivityPresetsResponse,
  AnalysisRequest,
  MetaResponse,
  Operation,
  OperationContract,
  PopulationMetadataResponse,
} from './types'

export async function fetchMeta(signal?: AbortSignal): Promise<MetaResponse> {
  return (await apiRequest<MetaResponse>('/meta', { signal })).data
}

export async function fetchPopulationMetadata(
  signal?: AbortSignal,
): Promise<PopulationMetadataResponse> {
  return (await apiRequest<PopulationMetadataResponse>('/population/metadata', { signal })).data
}

/** The full lexicon (~70 KB, 556 entries) — fetched once and cached per session. */
export async function fetchActivities(signal?: AbortSignal): Promise<ActivityListResponse> {
  return (await apiRequest<ActivityListResponse>('/activities', { signal })).data
}

export async function fetchActivityPresets(
  signal?: AbortSignal,
): Promise<ActivityPresetsResponse> {
  return (await apiRequest<ActivityPresetsResponse>('/activities/presets', { signal })).data
}

export async function fetchActivityDetail(
  code: string,
  signal?: AbortSignal,
): Promise<ActivityDetailResponse> {
  return (
    await apiRequest<ActivityDetailResponse>(`/activities/${encodeURIComponent(code)}`, {
      signal,
    })
  ).data
}

export async function runAnalysis<Op extends Operation>(
  operation: Op,
  spec: OperationContract[Op]['request'],
  signal?: AbortSignal,
): Promise<ApiResult<OperationContract[Op]['response']>> {
  return apiRequest<OperationContract[Op]['response']>(`/analysis/${operation}`, {
    method: 'POST',
    body: spec satisfies AnalysisRequest,
    signal,
  })
}
