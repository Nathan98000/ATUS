/**
 * Analysis specs for the k6 scenarios: the committed equivalence corpus, the
 * SPA's starters and prewarmed analyses, the worst cases, and deterministic
 * generators of distinct specs (to defeat the API's result cache).
 *
 * Spec source (environment):
 *   SPEC_DIR    directory of *.jsonl spec files (default: the committed §9
 *               corpus, docs/evidence/phase5/equivalence/specs). An absolute
 *               path is safest: a relative one resolves from this file's
 *               directory (k6 open() semantics).
 *   SPEC_FILES  comma-separated file names in SPEC_DIR (default: all five).
 *   GEN_SEED    generator seed (default 20261020).
 *
 * A corpus line is {"id", "endpoint", "body", "expect", ...}; only specs whose
 * expected outcome is "ok" are used.
 */
import { SharedArray } from 'k6/data'

import { envInt, envStr, hash32, prng } from './common.js'

const SPEC_DIR = envStr('SPEC_DIR', '../../../docs/evidence/phase5/equivalence/specs')
const SPEC_FILES = envStr(
  'SPEC_FILES',
  'compare-edge.jsonl,errors-weird.jsonl,measures-weights.jsonl,pooled-trends.jsonl,population.jsonl',
)
  .split(',')
  .map((name) => name.trim())
  .filter(Boolean)
export const GEN_SEED = envInt('GEN_SEED', 20261020)

function expectsOk(spec) {
  const expect = spec.expect
  if (expect === 'ok') return true
  return expect !== null && typeof expect === 'object' && expect.outcome === 'ok'
}

/** The corpus specs expected to succeed, as a plain array (init context only). */
function loadCorpus() {
  const out = []
  for (const file of SPEC_FILES) {
    const text = open(`${SPEC_DIR}/${file}`)
    for (const line of text.split('\n')) {
      if (!line.trim()) continue
      const spec = JSON.parse(line)
      if (!expectsOk(spec) || spec.body === null || typeof spec.body !== 'object') continue
      if (!Array.isArray(spec.body.years)) continue
      out.push({ id: spec.id, op: spec.endpoint.split('/').pop(), body: spec.body })
    }
  }
  return out
}

/** Corpus single-year estimates with replicate SEs (the uncached scenario's first specs). */
export const corpusSingleYear = new SharedArray('atus-corpus-single-year', function filter() {
  return loadCorpus().filter(
    (spec) => spec.op === 'estimate' && spec.body.years.length === 1 && spec.body.variance !== 'none',
  )
})

/** Corpus estimates, trends and compares over 22+ years with replicate SEs. */
export const corpusWorst = new SharedArray('atus-corpus-worst', function filter() {
  return loadCorpus().filter((spec) => spec.body.years.length >= 22 && spec.body.variance !== 'none')
})

/** A SharedArray as a plain array (SharedArray supports indexing, not every Array method). */
export function plain(shared) {
  const out = []
  for (let i = 0; i < shared.length; i++) out.push(shared[i])
  return out
}

// ------------------------------------------------------------------ fixed sets
/** The home page's four starter examples (frontend/src/domain/prewarm.ts). */
export function starters(ctx) {
  const latest = ctx.latest
  return [
    {
      id: 'starter-sleep',
      op: 'estimate',
      body: { measure: 'average_minutes_per_day', activity: { preset: 'sleep' }, years: [latest] },
    },
    {
      id: 'starter-leisure-trend',
      op: 'trend',
      body: {
        measure: 'average_minutes_per_day',
        activity: { preset: 'leisure_and_sports_bls_table' },
        years: ctx.years.slice(),
      },
    },
    {
      id: 'starter-tv-by-sex',
      op: 'compare',
      body: {
        measure: 'average_minutes_per_day',
        activity: { preset: 'watching_tv' },
        years: [latest],
        group_a: { sex: 'male' },
        group_b: { sex: 'female' },
        label_a: 'Men',
        label_b: 'Women',
      },
    },
    {
      id: 'starter-household-children',
      op: 'compare',
      body: {
        measure: 'average_minutes_per_day',
        activity: { preset: 'household_activities_bls_table' },
        years: [latest],
        group_a: { has_household_children: true },
        group_b: { has_household_children: false },
        label_a: 'Household children present',
        label_b: 'No household children',
      },
    },
  ]
}

const BY_SEX = { group_a: { sex: 'male' }, group_b: { sex: 'female' }, label_a: 'Men', label_b: 'Women' }

/**
 * The static snapshot's prewarm list (ADR-011 L0), in the same order and
 * coverage as frontend/src/domain/prewarm.ts: starters; every preset under
 * every measure (latest year); a full-period trend per preset and per tier-1
 * category; a latest-year estimate per tier-1 category; men vs women per
 * preset and per tier-1 category; each preset in 2019 and 2020 under the
 * pandemic weights. Bodies use the minimal request shape; the API's cache key
 * is the canonical spec, so the result-cache behaviour is the same.
 */
export function prewarmList(ctx) {
  const list = starters(ctx)
  const latest = ctx.latest
  const add = (id, op, body) => list.push({ id, op, body })
  for (const preset of ctx.presets) {
    for (const measure of ctx.measures) {
      add(`estimate-${preset}-${measure}`, 'estimate', { measure, activity: { preset }, years: [latest] })
    }
  }
  for (const preset of ctx.presets) {
    add(`trend-${preset}`, 'trend', {
      measure: 'average_minutes_per_day', activity: { preset }, years: ctx.years.slice(),
    })
  }
  for (const code of ctx.codes1) {
    add(`trend-${code}`, 'trend', {
      measure: 'average_minutes_per_day', activity: { include: [code] }, years: ctx.years.slice(),
    })
  }
  for (const code of ctx.codes1) {
    add(`estimate-${code}`, 'estimate', {
      measure: 'average_minutes_per_day', activity: { include: [code] }, years: [latest],
    })
  }
  for (const preset of ctx.presets) {
    add(`compare-sex-${preset}`, 'compare', Object.assign({
      measure: 'average_minutes_per_day', activity: { preset }, years: [latest],
    }, BY_SEX))
  }
  for (const code of ctx.codes1) {
    add(`compare-sex-${code}`, 'compare', Object.assign({
      measure: 'average_minutes_per_day', activity: { include: [code] }, years: [latest],
    }, BY_SEX))
  }
  if (ctx.weightSchemes.indexOf('pandemic') >= 0) {
    for (const year of [2019, 2020].filter((y) => ctx.years.indexOf(y) >= 0)) {
      for (const preset of ctx.presets) {
        add(`pandemic-${preset}-${year}`, 'estimate', {
          measure: 'average_minutes_per_day', activity: { preset }, years: [year], weights: 'pandemic',
        })
      }
    }
  }
  const seen = {}
  return list.filter((item) => {
    const key = `${item.op} ${JSON.stringify(item.body)}`
    if (seen[key]) return false
    seen[key] = true
    return true
  })
}

/**
 * The cached scenario's warm set:
 *   starters  the four starters;
 *   presets   (default) starters + each preset as a latest-year estimate and
 *             as men vs women (22 specs, one of them a 23-year trend);
 *   prewarm   the whole snapshot list (~130 specs, 27 of them 23-year trends:
 *             slow to warm at 0.25 CU).
 * Always fewer than the API's 256 cache entries.
 */
export function warmSet(ctx, name) {
  if (name === 'starters') return starters(ctx)
  if (name === 'prewarm') return prewarmList(ctx)
  if (name !== 'presets') throw new Error(`WARM_SET must be starters|presets|prewarm, got ${name}`)
  const list = starters(ctx)
  for (const preset of ctx.presets) {
    list.push({
      id: `estimate-${preset}`,
      op: 'estimate',
      body: { measure: 'average_minutes_per_day', activity: { preset }, years: [ctx.latest] },
    })
    list.push({
      id: `compare-sex-${preset}`,
      op: 'compare',
      body: Object.assign({
        measure: 'average_minutes_per_day', activity: { preset }, years: [ctx.latest],
      }, BY_SEX),
    })
  }
  return list
}

/**
 * Worst cases (ADR-011 "Measured facts", audit F66):
 *   W1 pooled 22 years, BLS leisure          (timed audit: pooled 22 years)
 *   W2 trend 23 years, tier-1 "12"           (timed audit)
 *   W3 trend 23 years, "010101"              (timed audit)
 *   W4 F66: all 18 tier-1 codes pooled over 22 years
 *   W5 F66: all 18 tier-1 codes as a 23-year trend
 *   W6 F66: all tier-1 codes pooled 22 years, men vs women (two statistics
 *      queries; WORST_SET=all only)
 * WORST_SET: core (W1-W5, default) | all (W1-W6) | corpus (the corpus's 22+
 * year specs) | core+corpus.
 */
export function worstList(ctx, name) {
  const tier1 = ctx.codes1.slice()
  const core = [
    {
      id: 'W1-pooled22-leisure',
      op: 'estimate',
      body: {
        measure: 'average_minutes_per_day',
        activity: { preset: 'leisure_and_sports_bls_table' },
        years: ctx.multiYears.slice(),
      },
    },
    {
      id: 'W2-trend23-12',
      op: 'trend',
      body: { measure: 'average_minutes_per_day', activity: { include: ['12'] }, years: ctx.years.slice() },
    },
    {
      id: 'W3-trend23-010101',
      op: 'trend',
      body: { measure: 'average_minutes_per_day', activity: { include: ['010101'] }, years: ctx.years.slice() },
    },
    {
      id: 'W4-F66-pooled22-tier1',
      op: 'estimate',
      body: {
        measure: 'average_minutes_per_day',
        activity: { include: tier1, label: 'All activities' },
        years: ctx.multiYears.slice(),
      },
    },
    {
      id: 'W5-F66-trend23-tier1',
      op: 'trend',
      body: {
        measure: 'average_minutes_per_day',
        activity: { include: tier1, label: 'All activities' },
        years: ctx.years.slice(),
      },
    },
  ]
  const w6 = {
    id: 'W6-F66-compare22-sex',
    op: 'compare',
    body: Object.assign({
      measure: 'average_minutes_per_day',
      activity: { include: tier1, label: 'All activities' },
      years: ctx.multiYears.slice(),
    }, BY_SEX),
  }
  const fromCorpus = plain(corpusWorst).map((spec) => ({ id: spec.id, op: spec.op, body: spec.body }))
  if (name === 'core') return core
  if (name === 'all') return core.concat([w6])
  if (name === 'corpus') return fromCorpus
  if (name === 'core+corpus') return core.concat(fromCorpus)
  throw new Error(`WORST_SET must be core|all|corpus|core+corpus, got ${name}`)
}

/**
 * A copy of `body` with a confidence level unique to `k` (k >= 1). The
 * confidence level only scales the interval after the statistics queries
 * (analytics/variance.py), so the database work is identical, but it is part
 * of the cache key: a repeated spec stays uncached (audit F15).
 */
export function salted(body, k) {
  const copy = JSON.parse(JSON.stringify(body))
  copy.confidence_level = Math.round((0.95 + k * 1e-7) * 1e10) / 1e10
  return copy
}

// ------------------------------------------------------------------ generators
const POPULATIONS = [
  {},
  { sex: 'female' },
  { sex: 'male' },
  { day_type: 'weekend' },
  { day_type: 'weekday' },
  { age_min: 25, age_max: 54 },
  { age_min: 65 },
  { employment_status: 'employed' },
  { has_household_children: true },
]

const GROUPS = [
  [{ sex: 'male' }, { sex: 'female' }, 'Men', 'Women'],
  [{ has_household_children: true }, { has_household_children: false }, 'Children', 'No children'],
  [{ day_type: 'weekday' }, { day_type: 'weekend' }, 'Weekdays', 'Weekends'],
  [{ employment_status: 'employed' }, { employment_status: 'not_in_labor_force' }, 'Employed', 'Not in LF'],
  [{ region: 1 }, { region: 3 }, 'Northeast', 'South'],
]

function gcd(a, b) {
  while (b) {
    const t = a % b
    a = b
    b = t
  }
  return a
}

const STRIDES = [104729, 7919, 15485863, 2147483629]
/** n -> a permutation of [0, size): distinct n below size give distinct results. */
function permute(n, size, salt) {
  let stride = 1
  for (const candidate of STRIDES) {
    if (gcd(candidate % size || size, size) === 1) {
      stride = candidate % size || 1
      break
    }
  }
  return ((n % size) * stride + (salt % size)) % size
}

function digits(index, radices) {
  const out = []
  let rest = index
  for (const radix of radices) {
    out.push(rest % radix)
    rest = Math.floor(rest / radix)
  }
  return out
}

function activityFor(choice) {
  return choice.preset ? { preset: choice.preset } : { include: [choice.code] }
}

/** Presets and 2- and 4-digit codes: broad selections with data in every year. */
function broadChoices(ctx) {
  return ctx.presets.map((preset) => ({ preset })).concat(
    ctx.codes1.concat(ctx.codes2).map((code) => ({ code })),
  )
}

/** Every preset and lexicon code (6-digit codes included). */
function allChoices(ctx) {
  return broadChoices(ctx).concat(ctx.codes3.map((code) => ({ code })))
}

function populationBody(population) {
  return Object.keys(population).length ? { population } : {}
}

/** Generator space sizes, for documentation and exhaustion checks. */
export function generatorSizes(ctx) {
  const broad = broadChoices(ctx).length
  const all = allChoices(ctx).length
  return {
    singleYear: broad * ctx.multiYears.length * ctx.measures.length * POPULATIONS.length,
    compare: broad * ctx.multiYears.length * ctx.measures.length * GROUPS.length,
    trend: all * ctx.measures.length * POPULATIONS.length,
    pooled: all * ctx.measures.length * windows(ctx).length,
  }
}

/**
 * The n-th distinct single-year estimate with replicate SEs: a broad
 * selection x a multi-year-weight year x a measure x a population. Distinct
 * n below generatorSizes().singleYear give distinct specs.
 */
export function genSingleYear(n, ctx, salt) {
  const choices = broadChoices(ctx)
  const radices = [choices.length, ctx.multiYears.length, ctx.measures.length, POPULATIONS.length]
  const size = radices.reduce((a, b) => a * b, 1)
  const [c, y, m, p] = digits(permute(n, size, GEN_SEED + (salt || 0)), radices)
  return {
    id: `gen1-${n}`,
    op: 'estimate',
    body: Object.assign(
      { measure: ctx.measures[m], activity: activityFor(choices[c]), years: [ctx.multiYears[y]] },
      populationBody(POPULATIONS[p]),
    ),
  }
}

function genCompare(n, ctx) {
  const choices = broadChoices(ctx)
  const radices = [choices.length, ctx.multiYears.length, ctx.measures.length, GROUPS.length]
  const size = radices.reduce((a, b) => a * b, 1)
  const [c, y, m, g] = digits(permute(n, size, GEN_SEED + 11), radices)
  const group = GROUPS[g]
  return {
    id: `genc-${n}`,
    op: 'compare',
    body: {
      measure: ctx.measures[m],
      activity: activityFor(choices[c]),
      years: [ctx.multiYears[y]],
      group_a: group[0],
      group_b: group[1],
      label_a: group[2],
      label_b: group[3],
    },
  }
}

function genTrend(n, ctx) {
  const choices = allChoices(ctx)
  const radices = [choices.length, ctx.measures.length, POPULATIONS.length]
  const size = radices.reduce((a, b) => a * b, 1)
  const [c, m, p] = digits(permute(n, size, GEN_SEED + 23), radices)
  return {
    id: `gent-${n}`,
    op: 'trend',
    body: Object.assign(
      { measure: ctx.measures[m], activity: activityFor(choices[c]), years: ctx.years.slice() },
      populationBody(POPULATIONS[p]),
    ),
  }
}

/** Contiguous windows of 8, 12 and 22 multi-year-weight years: all "heavy" (>= 8). */
function windows(ctx) {
  const out = []
  for (const length of [8, 12, 22]) {
    for (let start = 0; start + length <= ctx.multiYears.length; start++) {
      out.push(ctx.multiYears.slice(start, start + length))
    }
  }
  return out
}

function genPooled(n, ctx) {
  const choices = allChoices(ctx)
  const spans = windows(ctx)
  const radices = [choices.length, ctx.measures.length, spans.length]
  const size = radices.reduce((a, b) => a * b, 1)
  const [c, m, w] = digits(permute(n, size, GEN_SEED + 37), radices)
  return {
    id: `genp-${n}`,
    op: 'estimate',
    body: { measure: ctx.measures[m], activity: activityFor(choices[c]), years: spans[w] },
  }
}

/**
 * A novel analysis for the demo mix. The traffic model (docs/deployment.md)
 * says 30% of uncached analyses are multi-year ("heavy"); the split inside
 * each class is this harness's assumption:
 *   light: 5/7 single-year estimates, 2/7 single-year compares;
 *   heavy: 1/2 full-period trends, 1/2 pooled estimates over 8-22 years.
 * `n` must be unique per novel view (it indexes the generators).
 */
export function novelSpec(n, heavy, draw, ctx) {
  if (heavy) return draw < 0.5 ? genTrend(n, ctx) : genPooled(n, ctx)
  return draw < 5 / 7 ? genSingleYear(n, ctx, 101) : genCompare(n, ctx)
}

/** Seeded per-item PRNG. */
export function rngFor(seed, index) {
  return prng(hash32(seed, index))
}
