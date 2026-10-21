/**
 * Scenario: metadata (brief §13).
 *
 * Each iteration sends the four metadata reads the SPA makes when it is not
 * served from the static snapshot: GET /api/v1/meta, /api/v1/activities,
 * /api/v1/activities/<code> (cycling through 2-, 4- and 6-digit codes) and
 * /api/v1/population/metadata. With the in-process data snapshot fresh (ADR-012)
 * these cost no SQL statement, so this measures the API process alone.
 *
 * Environment (besides lib/common.js): EXECUTOR (default iterations), VUS,
 * ITERATIONS, DURATION, ROTATE_EVERY (requests per client address; default
 * 100, under the 120/min request bucket), THINK_S (pause between iterations;
 * default 0).
 *
 *   ATUS_LT_EDGE_SECRET=... k6 run -e BASE_URL=http://127.0.0.1:8701 \
 *     -e EXECUTOR=duration -e VUS=4 -e DURATION=60s loadtests/k6/metadata.js
 */
import { sleep } from 'k6'

import {
  apiRequest,
  buildOptions,
  envFloat,
  envInt,
  fetchContext,
  iterationInTest,
  makeHandleSummary,
  vuClientIp,
} from './lib/common.js'

export const options = buildOptions({
  executor: 'iterations',
  vus: 1,
  iterations: 5,
  duration: '60s',
  endpoints: ['meta', 'activities', 'activity_detail', 'population_metadata'],
})

const ROTATE = envInt('ROTATE_EVERY', 100)
const THINK_S = envFloat('THINK_S', 0)

export function setup() {
  return fetchContext()
}

export default function metadata(ctx) {
  const i = Math.max(iterationInTest(), 0)
  const code = ctx.detailCodes[i % ctx.detailCodes.length]
  apiRequest({ path: '/api/v1/meta', ep: 'meta', ip: vuClientIp(ROTATE) })
  apiRequest({ path: '/api/v1/activities', ep: 'activities', ip: vuClientIp(ROTATE) })
  apiRequest({
    path: `/api/v1/activities/${code}`,
    ep: 'activity_detail',
    kind: `level${code.length / 2}`,
    ip: vuClientIp(ROTATE),
  })
  apiRequest({ path: '/api/v1/population/metadata', ep: 'population_metadata', ip: vuClientIp(ROTATE) })
  if (THINK_S > 0) sleep(THINK_S)
}

export const handleSummary = makeHandleSummary('metadata')
