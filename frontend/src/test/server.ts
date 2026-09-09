/**
 * MSW mock of the Phase 3 API for fast tests. Default handlers serve the
 * captured fixtures; individual tests override with `server.use(...)`.
 */
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'

import { apiBaseUrl } from '../api/client'
import {
  activitiesFixture,
  activityDetailFixture,
  compareChildrenFixture,
  estimateSleepFixture,
  metaFixture,
  populationMetadataFixture,
  presetsFixture,
  trendLeisureFixture,
} from './fixtures'

export const api = (path: string) => `${apiBaseUrl()}/api/v1${path}`

export const defaultHandlers = [
  http.get(api('/meta'), () => HttpResponse.json(metaFixture)),
  http.get(api('/population/metadata'), () => HttpResponse.json(populationMetadataFixture)),
  http.get(api('/activities'), () => HttpResponse.json(activitiesFixture)),
  http.get(api('/activities/presets'), () => HttpResponse.json(presetsFixture)),
  http.get(api('/activities/:code'), () => HttpResponse.json(activityDetailFixture)),
  http.post(api('/analysis/estimate'), () =>
    HttpResponse.json(estimateSleepFixture, { headers: analysisHeaders() }),
  ),
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
