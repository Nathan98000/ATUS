/**
 * Scenario: uncached single-year estimates with replicate SEs (brief §13).
 *
 * Every request is a distinct spec, so none can be answered from the API's
 * result cache: first the committed corpus's single-year estimates with
 * replicate SEs (SOURCE=both or corpus), then the deterministic generator
 * (a broad activity selection x a year x a measure x a population; tens of
 * thousands of distinct specs). If the list is exhausted it starts again with
 * a unique confidence level per pass (same SQL, new cache key; lib/specs.js
 * salted()).
 *
 * Environment (besides lib/common.js and lib/specs.js):
 *   SOURCE        both (default) | corpus | generator
 *   OFFSET        start index into the list (default 0): runs against the
 *                 same API process without a restart should use a new offset
 *   EXECUTOR, VUS, ITERATIONS, DURATION, STAGES; THINK_S (default 0);
 *   ROTATE_EVERY  analyses per client address (default 5, under the burst of
 *                 6 in the per-client analysis bucket)
 * Accepted outcomes: 200, and 422 insufficient_data (a generated selection can
 * be too rare in a population; it still ran its statistics query).
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
import { corpusSingleYear, genSingleYear, generatorSizes, plain, salted } from './lib/specs.js'

export const options = buildOptions({
  executor: 'iterations',
  vus: 1,
  iterations: 5,
  duration: '60s',
  endpoints: ['estimate'],
})

const ROTATE = envInt('ROTATE_EVERY', 5)
const THINK_S = envFloat('THINK_S', 0)
const OFFSET = envInt('OFFSET', 0)
const SOURCE = envStr('SOURCE', 'both')
if (['both', 'corpus', 'generator'].indexOf(SOURCE) < 0) {
  throw new Error(`SOURCE must be both|corpus|generator, got ${SOURCE}`)
}
const CORPUS = SOURCE === 'generator' ? [] : plain(corpusSingleYear)

export function setup() {
  const ctx = fetchContext()
  const generated = SOURCE === 'corpus' ? 0 : generatorSizes(ctx).singleYear
  return { ctx, listSize: CORPUS.length + generated, corpusSize: CORPUS.length }
}

function specAt(index, data) {
  const pass = Math.floor(index / data.listSize)
  const i = index % data.listSize
  const spec = i < CORPUS.length ? CORPUS[i] : genSingleYear(i - CORPUS.length, data.ctx, 0)
  if (pass === 0) return spec
  return { id: `${spec.id}~${pass}`, op: spec.op, body: salted(spec.body, pass) }
}

export default function uncached(data) {
  const spec = specAt(OFFSET + Math.max(iterationInTest(), 0), data)
  apiRequest({
    method: 'POST',
    path: '/api/v1/analysis/estimate',
    body: spec.body,
    ep: 'estimate',
    kind: spec.id.indexOf('gen') === 0 ? 'generated' : 'corpus',
    spec: spec.id,
    expect: ['200', '422:insufficient_data'],
    expectCache: 'miss',
    ip: vuClientIp(ROTATE),
  })
  if (THINK_S > 0) sleep(THINK_S)
}

export const handleSummary = makeHandleSummary('uncached')
