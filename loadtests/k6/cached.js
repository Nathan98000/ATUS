/**
 * Scenario: cached analyses (brief §13).
 *
 * setup() computes a warm set once (each spec sent once; phase "setup", not
 * part of the measurement), then every iteration repeats one of the specs that
 * answered 200, so each main-phase request should be an API cache hit
 * (X-Cache: hit; counted in atus_unexpected_cache otherwise). With the data
 * snapshot fresh, a hit runs no SQL (ADR-012), so this measures the API's own
 * cost of validation, cache lookup and response.
 *
 * Environment (besides lib/common.js):
 *   WARM_SET      starters | presets (default; 22 specs) | prewarm (~130, slow
 *                 at 0.25 CU) — see lib/specs.js warmSet()
 *   ORDER         cycle (default: spec i mod n) | random (seeded by GEN_SEED)
 *   EXECUTOR, VUS, ITERATIONS, DURATION; ROTATE_EVERY (default 100: hits are
 *   charged to the 120/min request bucket only); THINK_S (default 0)
 */
import { sleep } from 'k6'

import {
  apiRequest,
  buildOptions,
  envFloat,
  envInt,
  envStr,
  fetchContext,
  iterationInTest,
  makeHandleSummary,
  vuClientIp,
} from './lib/common.js'
import { GEN_SEED, rngFor, warmSet } from './lib/specs.js'

export const options = buildOptions({
  executor: 'iterations',
  vus: 1,
  iterations: 10,
  duration: '60s',
  endpoints: ['estimate', 'trend', 'compare'],
})

const ROTATE = envInt('ROTATE_EVERY', 100)
const THINK_S = envFloat('THINK_S', 0)
const ORDER = envStr('ORDER', 'cycle')

export function setup() {
  const ctx = fetchContext()
  const candidates = warmSet(ctx, envStr('WARM_SET', 'presets'))
  const warm = []
  const refused = []
  for (const spec of candidates) {
    const r = apiRequest({
      method: 'POST',
      path: `/api/v1/analysis/${spec.op}`,
      body: spec.body,
      ep: spec.op,
      kind: 'warm',
      phase: 'setup',
      spec: spec.id,
      ip: vuClientIp(5),
    })
    if (r.status === 200) warm.push(spec)
    else refused.push({ id: spec.id, status: r.status, code: r.code })
  }
  if (warm.length === 0) throw new Error('setup: no warm-set spec answered 200')
  return { ctx, warm, refused }
}

export default function cached(data) {
  const i = Math.max(iterationInTest(), 0)
  const index =
    ORDER === 'random' ? Math.floor(rngFor(GEN_SEED, i)() * data.warm.length) : i % data.warm.length
  const spec = data.warm[index]
  apiRequest({
    method: 'POST',
    path: `/api/v1/analysis/${spec.op}`,
    body: spec.body,
    ep: spec.op,
    kind: 'cached',
    spec: spec.id,
    expectCache: 'hit',
    ip: vuClientIp(ROTATE),
  })
  if (THINK_S > 0) sleep(THINK_S)
}

export const handleSummary = makeHandleSummary('cached')
