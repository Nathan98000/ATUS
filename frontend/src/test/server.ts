/**
 * MSW mock of the Phase 3 API for fast tests. Default handlers serve the
 * captured fixtures; individual tests override with `server.use(...)`.
 * There is no static snapshot by default (`/snap/current.json` is a 404, as in
 * development); `installSnapshot` publishes one for snapshot tests.
 */
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'

import { apiBaseUrl } from '../api/client'
import { SNAPSHOT_POINTER_PATH, requestKey } from '../api/snapshot'
import {
  activitiesFixture,
  activityDetailFixture,
  compareChildrenFixture,
  estimatePandemicFixture,
  estimateSleepFixture,
  metaFixture,
  populationMetadataFixture,
  presetsFixture,
  trendLeisureFixture,
} from './fixtures'

export const api = (path: string) => `${apiBaseUrl()}/api/v1${path}`

export const defaultHandlers = [
  http.get(SNAPSHOT_POINTER_PATH, () => new HttpResponse(null, { status: 404 })),
  http.get(api('/meta'), () => HttpResponse.json(metaFixture)),
  http.get(api('/population/metadata'), () => HttpResponse.json(populationMetadataFixture)),
  http.get(api('/activities'), () => HttpResponse.json(activitiesFixture)),
  http.get(api('/activities/presets'), () => HttpResponse.json(presetsFixture)),
  http.get(api('/activities/:code'), () => HttpResponse.json(activityDetailFixture)),
  // A trend's 2020 asks for the pandemic weights: the captured 2020 estimate.
  http.post(api('/analysis/estimate'), async ({ request }) => {
    const body = (await request.clone().json()) as { weights?: string }
    const fixture = body.weights === 'pandemic' ? estimatePandemicFixture : estimateSleepFixture
    return HttpResponse.json(fixture, { headers: analysisHeaders() })
  }),
  http.post(api('/analysis/trend'), () =>
    HttpResponse.json(trendLeisureFixture, { headers: analysisHeaders() }),
  ),
  http.post(api('/analysis/compare'), () =>
    HttpResponse.json(compareChildrenFixture, { headers: analysisHeaders() }),
  ),
]

export function analysisHeaders(cache: 'hit' | 'miss' = 'miss'): Record<string, string> {
  return {
    'X-Analysis-Key': 'a'.repeat(64),
    'X-Cache': cache,
    'X-Request-ID': 'test-request-id',
  }
}

export function errorResponse(
  status: number,
  code: string,
  message: string,
  details: unknown = null,
) {
  return HttpResponse.json(
    { error: { code, message, details } },
    { status, headers: { 'X-Request-ID': 'test-request-id' } },
  )
}

export const server = setupServer(...defaultHandlers)

export const SNAPSHOT_ID = 'abcdef123456-0123456789abcdef'
export const SNAPSHOT_DATA_VERSION = '0325:serving-20261020T120000Z-0a1b2c3d:00112233aabb'

export interface SnapshotPostFixture {
  path: string
  /** The exact request body the SPA would send (stableStringify of the spec). */
  body: string
  response: unknown
  analysisKey?: string
}

/**
 * Publishes a static snapshot through MSW, laid out as the deploy writes it:
 * the pointer, an immutable manifest, and one file per response. Returns the
 * list of snapshot file paths that were requested, for assertions.
 */
export async function installSnapshot({
  get = {},
  post = [],
  dataVersion = SNAPSHOT_DATA_VERSION,
}: {
  get?: Record<string, unknown>
  post?: SnapshotPostFixture[]
  dataVersion?: string
}): Promise<string[]> {
  const files = new Map<string, unknown>()
  const manifest = {
    snapshot_id: SNAPSHOT_ID,
    data_version: dataVersion,
    analytics_version: '0.2',
    get: {} as Record<string, string>,
    post: {} as Record<string, unknown>,
  }
  Object.entries(get).forEach(([path, body], index) => {
    const file = `get/${index}.json`
    manifest.get[path] = file
    files.set(file, body)
  })
  for (const entry of post) {
    const key = (await requestKey(entry.path, '', entry.body)) as string
    const file = `post/${key}.json`
    manifest.post[key] = {
      path: entry.path,
      file,
      analysis_key: entry.analysisKey ?? 'b'.repeat(64),
      x_cache: 'miss',
    }
    files.set(file, entry.response)
  }
  const requested: string[] = []
  server.use(
    http.get(SNAPSHOT_POINTER_PATH, () =>
      HttpResponse.json({
        snapshot_id: SNAPSHOT_ID,
        data_version: dataVersion,
        analytics_version: '0.2',
        api_version: 'v1',
        response_format: '1',
        generated_at: '2026-10-20T12:00:00Z',
      }),
    ),
    http.get(`/snap/${SNAPSHOT_ID}/*`, ({ request }) => {
      const file = new URL(request.url).pathname.slice(`/snap/${SNAPSHOT_ID}/`.length)
      requested.push(file)
      if (file === 'manifest.json') return HttpResponse.json(manifest)
      // A missing file is answered like the real host: the SPA's index.html, 200.
      if (!files.has(file)) return new HttpResponse('<!doctype html><title>x</title>')
      return HttpResponse.json(files.get(file) as Record<string, unknown>)
    }),
  )
  return requested
}
