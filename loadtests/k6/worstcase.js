/**
 * Scenario: worst cases (brief §13; ADR-011 "F66 broad selectors added to the
 * §13 emulation matrix").
 *
 * Cycles through the worst-case list (lib/specs.js worstList): pooled 22-year
 * estimates, 23-year trends and the F66 broad selectors (all 18 tier-1 codes
 * pooled and trended; WORST_SET=all adds the F66 compare by sex). Every
 * request carries a confidence level unique to its iteration by default
 * (SALT=on), so a repeated worst case is recomputed, never a cache hit: the
 * SQL is identical, only the cache key differs.
 *
 * These are the requests that size statement_timeout (ADR-012 "Calibration":
 * clamp(3 x p99, 30 s, 85 s)). Run them with VUS=1 for the latency of one
 * request at a time; with more VUs the API's heavy lane (1) sheds the rest as
 * 503 overloaded after ATUS_API_ADMISSION_WAIT_S, which is the protection
 * working, and is recorded as such. To see latencies beyond the production
 * 85 s timeout, run.py --statement-timeout-ms raises it for the load run.
 *
 * Environment (besides lib/common.js): WORST_SET (core | all | corpus |
 * core+corpus), SALT (on), OFFSET (0), EXECUTOR, VUS, ITERATIONS, DURATION,
 * THINK_S (0), ROTATE_EVERY (5), REQ_TIMEOUT (200s).
 */
import { sleep } from 'k6'

import {
  apiRequest,
  buildOptions,
  envBool,
  envFloat,
  envInt,
  envStr,
  fetchContext,
  iterationInTest,
  makeHandleSummary,
  vuClientIp,
} from './lib/common.js'
import { salted, worstList } from './lib/specs.js'

export const options = buildOptions({
  executor: 'iterations',
  vus: 1,
  iterations: 5,
  duration: '10m',
  endpoints: ['estimate', 'trend', 'compare'],
})

const ROTATE = envInt('ROTATE_EVERY', 5)
const THINK_S = envFloat('THINK_S', 0)
const OFFSET = envInt('OFFSET', 0)
const SALT = envBool('SALT', true)
const WORST_SET = envStr('WORST_SET', 'core')

export function setup() {
  const ctx = fetchContext()
  return { ctx, list: worstList(ctx, WORST_SET) }
}

export default function worstcase(data) {
  const i = OFFSET + Math.max(iterationInTest(), 0)
  const spec = data.list[i % data.list.length]
  apiRequest({
    method: 'POST',
    path: `/api/v1/analysis/${spec.op}`,
    body: SALT ? salted(spec.body, i + 1) : spec.body,
    ep: spec.op,
    kind: spec.id,
    spec: spec.id,
    expectCache: SALT ? 'miss' : undefined,
    ip: vuClientIp(ROTATE),
  })
  if (THINK_S > 0) sleep(THINK_S)
}

export const handleSummary = makeHandleSummary('worstcase')
