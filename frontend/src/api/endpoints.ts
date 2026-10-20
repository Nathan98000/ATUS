/**
 * Typed functions for every API endpoint the frontend uses.
 *
 * Each read is served, in order, by:
 *
 * 1. the static snapshot, when its manifest lists the request (all metadata
 *    and the prewarmed analyses of the deployed release; `snapshot.ts`);
 * 2. the API, through `withRetries`: time-budgeted retries while a sleeping
 *    free-tier host wakes up (`resilience.ts`).
 *
 * Analysis bodies are serialized once, with `stableStringify`, and those exact
 * bytes are both hashed for the snapshot lookup and sent to the API — so the
 * same specification always produces the same edge-cache and snapshot key.
 */
import { API_VERSION_PREFIX, apiRequest, type ApiResult } from './client'
import { checkMetaCompatibility } from './compat'
import { prewake } from './prewake'
import { COLD_START_BUDGET_MS, withRetries } from './resilience'
import { setLoadedDataVersion } from './serviceStatus'
import { snapshotGet, snapshotPost } from './snapshot'
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
import { stableStringify } from '../utils/stableStringify'

/** A single metadata attempt may wait this long (a waking host can hold a request). */
const METADATA_ATTEMPT_TIMEOUT_MS = 60_000
/** Guards against a connection that never answers; real analyses end far sooner. */
const ANALYSIS_ATTEMPT_TIMEOUT_MS = COLD_START_BUDGET_MS

async function getMetadata<T>(
  path: string,
  signal?: AbortSignal,
  cache?: RequestCache,
): Promise<ApiResult<T>> {
  const fromSnapshot = await snapshotGet<T>(API_VERSION_PREFIX + path)
  if (fromSnapshot) return fromSnapshot
  return withRetries(
    () => apiRequest<T>(path, { signal, cache, timeoutMs: METADATA_ATTEMPT_TIMEOUT_MS }),
    { kind: 'metadata', signal },
  )
}

/** `/meta` plus the data version that every result on the page is tagged with. */
export interface LoadedMeta {
  meta: MetaResponse
  /**
   * The data release identifier: `X-Data-Version` (or the snapshot's version),
   * else a `data_version` field in the body, else the legacy
   * `<release>:run<id>` tag of APIs that report neither.
   */
  dataVersion: string
}

function bodyDataVersion(body: unknown): string | null {
  const record = body as { data_version?: unknown; data?: { data_version?: unknown } } | null
  const value = record?.data_version ?? record?.data?.data_version
  return typeof value === 'string' && value !== '' ? value : null
}

export async function fetchMeta(signal?: AbortSignal): Promise<LoadedMeta> {
  // `no-cache`: always revalidate, so a reload never pairs a /meta from the
  // browser's HTTP cache with data from a newer release (audit F27).
  const result = await getMetadata<unknown>('/meta', signal, 'no-cache')
  const meta = checkMetaCompatibility(result.data)
  const reported = result.dataVersion ?? bodyDataVersion(result.data)
  const dataVersion = reported ?? `${meta.data.release}:run${meta.data.ingestion_run_id ?? 0}`
  setLoadedDataVersion(dataVersion, reported !== null)
  return { meta, dataVersion }
}

export async function fetchPopulationMetadata(
  signal?: AbortSignal,
): Promise<PopulationMetadataResponse> {
  return (await getMetadata<PopulationMetadataResponse>('/population/metadata', signal)).data
}

/** The full lexicon (~70 KB, 556 entries) — fetched once and cached per session. */
export async function fetchActivities(signal?: AbortSignal): Promise<ActivityListResponse> {
  return (await getMetadata<ActivityListResponse>('/activities', signal)).data
}

export async function fetchActivityPresets(
  signal?: AbortSignal,
): Promise<ActivityPresetsResponse> {
  return (await getMetadata<ActivityPresetsResponse>('/activities/presets', signal)).data
}

export async function fetchActivityDetail(
  code: string,
  signal?: AbortSignal,
): Promise<ActivityDetailResponse> {
  return (
    await getMetadata<ActivityDetailResponse>(`/activities/${encodeURIComponent(code)}`, signal)
  ).data
}

export async function runAnalysis<Op extends Operation>(
  operation: Op,
  spec: OperationContract[Op]['request'],
  signal?: AbortSignal,
): Promise<ApiResult<OperationContract[Op]['response']>> {
  type Response = OperationContract[Op]['response']
  const path = `/analysis/${operation}`
  const body = stableStringify(spec satisfies AnalysisRequest)

  const fromSnapshot = await snapshotPost<Response>(API_VERSION_PREFIX + path, body)
  if (fromSnapshot) return fromSnapshot

  // Not prewarmed: this analysis needs the API, which may be asleep.
  void prewake()
  return withRetries(
    () =>
      apiRequest<Response>(path, {
        method: 'POST',
        jsonBody: body,
        signal,
        timeoutMs: ANALYSIS_ATTEMPT_TIMEOUT_MS,
      }),
    { kind: 'analysis', signal },
  )
}
