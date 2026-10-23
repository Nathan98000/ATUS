/**
 * Scenario: realistic demo mix (brief §13), following the traffic model in
 * docs/deployment.md ("Realistic demo traffic"). Every iteration is one
 * visit, from its own client address. Per visit, on average:
 *
 *   3 analyses viewed: 1.5 from the static snapshot (starters and presets),
 *     0.5 share-link revisits (KV hits in production), 1.0 uncached (novel);
 *   30% of uncached analyses are multi-year ("heavy");
 *   0.5 wake calls (the SPA's prewake reaches the origin's /health).
 *
 * MIX_MODE decides which of those views reach the API:
 *   api     (default) every view, as on a stack without the edge tiers: the
 *           visit loads /meta, /activities and /population/metadata (and one
 *           activity detail half the time), starters and presets come from the
 *           API's cache once computed, revisits re-request an earlier visit's
 *           novel spec (hit, join or miss: whatever the API's LRU says);
 *   origin  only what reaches the API behind the ADR-011 edge: novel analyses
 *           and wake calls (snapshot views, metadata and KV-hit revisits are
 *           answered by Cloudflare). ORIGIN_KV_MISS (default 0) sends that
 *           share of revisits to the origin anyway (KV eventual consistency).
 *
 * The visit plan is a pure function of (GEN_SEED, visit number), so every run
 * draws the same visits, and a revisit can find the novel spec an earlier
 * visit drew. The probabilities are the traffic model's; how a novel view
 * splits into estimates, compares, trends and pooled ranges is an assumption
 * of this harness (lib/specs.js novelSpec).
 *
 * Environment (besides lib/common.js): MIX_MODE; EXECUTOR (default arrival:
 * RATE visits per TIME_UNIT=1m for DURATION, PRE_VUS, MAX_VUS); THINK_S
 * (mean pause between views, default 2; jittered +-50%); REVISIT_WINDOW
 * (how many visits back a revisit looks, default 50); OFFSET (first visit
 * number, default 0).
 */
import { sleep } from 'k6'

import {
  apiRequest,
  buildOptions,
  envFloat,
  envInt,
  envStr,
  fetchContext,
  ipFromIndex,
  iterationInTest,
  makeHandleSummary,
} from './lib/common.js'
import { GEN_SEED, novelSpec, prewarmList, rngFor, starters } from './lib/specs.js'

export const options = buildOptions({
  executor: 'arrival',
  vus: 2,
  rate: 2,
  timeUnit: '1m',
  duration: '10m',
  endpoints: ['health', 'meta', 'activities', 'activity_detail', 'population_metadata',
    'estimate', 'trend', 'compare'],
})

const MODE = envStr('MIX_MODE', 'api')
if (MODE !== 'api' && MODE !== 'origin') throw new Error(`MIX_MODE must be api|origin, got ${MODE}`)
const THINK_S = envFloat('THINK_S', 2)
const REVISIT_WINDOW = envInt('REVISIT_WINDOW', 50)
const ORIGIN_KV_MISS = envFloat('ORIGIN_KV_MISS', 0)
const OFFSET = envInt('OFFSET', 0)

// docs/deployment.md "Realistic demo traffic"
const VIEWS_PER_VISIT = 3
const P_SNAPSHOT = 1.5 / 3
const P_REVISIT = 0.5 / 3
const P_HEAVY = 0.3
const P_WAKE = 0.5
// this harness's assumptions
const P_STARTER_OF_SNAPSHOT = 0.4
const P_DETAIL = 0.5

export function setup() {
  const ctx = fetchContext()
  return { ctx, starters: starters(ctx), snapshot: prewarmList(ctx).slice(4) }
}

/** The visit's plan: a pure function of the seed and the visit number. */
function planVisit(v, data) {
  const rnd = rngFor(GEN_SEED, v)
  const plan = {
    wake: rnd() < P_WAKE,
    detail: rnd() < P_DETAIL ? data.ctx.detailCodes[Math.floor(rnd() * data.ctx.detailCodes.length)] : null,
    views: [],
  }
  for (let j = 0; j < VIEWS_PER_VISIT; j++) {
    const r = rnd()
    if (r < P_SNAPSHOT) {
      const starter = rnd() < P_STARTER_OF_SNAPSHOT
      const pool = starter ? data.starters : data.snapshot
      plan.views.push({ type: starter ? 'starter' : 'preset', spec: pool[Math.floor(rnd() * pool.length)] })
    } else if (r < P_SNAPSHOT + P_REVISIT) {
      plan.views.push({ type: 'revisit', back: 1 + Math.floor(rnd() * REVISIT_WINDOW), kvMiss: rnd() })
    } else {
      const heavy = rnd() < P_HEAVY
      plan.views.push({
        type: heavy ? 'novel_heavy' : 'novel_light',
        spec: novelSpec(v * VIEWS_PER_VISIT + j, heavy, rnd(), data.ctx),
      })
    }
  }
  return plan
}

/** The first novel spec of an earlier visit, looking back from v - back. */
function revisitSpec(v, back, data) {
  for (let u = v - back; u >= Math.max(0, v - back - 10); u--) {
    const earlier = planVisit(u, data)
    for (const view of earlier.views) {
      if (view.type === 'novel_light' || view.type === 'novel_heavy') return view.spec
    }
  }
  return null
}

function think(rnd) {
  if (THINK_S > 0) sleep(THINK_S * (0.5 + rnd()))
}

function analysis(spec, kind, ip) {
  apiRequest({
    method: 'POST',
    path: `/api/v1/analysis/${spec.op}`,
    body: spec.body,
    ep: spec.op,
    kind,
    spec: spec.id,
    expect: ['200', '422:insufficient_data'],
    ip,
  })
}

export default function mix(data) {
  const v = OFFSET + Math.max(iterationInTest(), 0)
  const plan = planVisit(v, data)
  const ip = ipFromIndex(v)
  const rnd = rngFor(GEN_SEED + 1, v)
  if (plan.wake) apiRequest({ path: '/health', ep: 'health', kind: 'wake', ip })
  if (MODE === 'api') {
    apiRequest({ path: '/api/v1/meta', ep: 'meta', ip })
    apiRequest({ path: '/api/v1/activities', ep: 'activities', ip })
    apiRequest({ path: '/api/v1/population/metadata', ep: 'population_metadata', ip })
    if (plan.detail) apiRequest({ path: `/api/v1/activities/${plan.detail}`, ep: 'activity_detail', ip })
  }
  for (const view of plan.views) {
    if (view.type === 'starter' || view.type === 'preset') {
      if (MODE !== 'api') continue
      think(rnd)
      analysis(view.spec, view.type, ip)
    } else if (view.type === 'revisit') {
      if (MODE === 'origin' && view.kvMiss >= ORIGIN_KV_MISS) continue
      const spec = revisitSpec(v, view.back, data)
      think(rnd)
      if (spec) analysis(spec, 'revisit', ip)
      else analysis(data.starters[0], 'revisit_fallback', ip)
    } else {
      think(rnd)
      analysis(view.spec, view.type, ip)
    }
  }
}

export const handleSummary = makeHandleSummary('mix')
