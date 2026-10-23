/**
 * Shared plumbing for the ATUS k6 scenarios (loadtests/README.md).
 *
 * Every request:
 *   - goes to BASE_URL directly, standing in for the Cloudflare Worker
 *     (ADR-011): it carries X-ATUS-Edge-Auth (the edge secret, read from the
 *     environment variable named by EDGE_SECRET_ENV, default
 *     ATUS_LT_EDGE_SECRET; never from the command line) and an
 *     X-ATUS-Client-IP from the benchmarking range 198.18.0.0/15, distinct per
 *     VU, so the per-client buckets of ADR-012 see many visitors instead of one;
 *   - is tagged with its endpoint (`ep`), the kind of view (`kind`) and the
 *     phase (`setup` or `main`), and records its status, error code, X-Cache,
 *     Server-Timing db/app and latency in custom metrics with the same tags;
 *   - writes one JSON line ({"t":"req", ...}) through console.log. run.py
 *     starts k6 with `--log-format=raw --console-output=<file>`, which makes
 *     that file the per-request record analyze.py reads.
 *
 * Nothing here prints the edge secret, and setup() data never holds it.
 */
import http from 'k6/http'
import exec from 'k6/execution'
import { check } from 'k6'
import { Counter, Rate, Trend } from 'k6/metrics'

// ------------------------------------------------------------------ settings
export function envStr(name, fallback) {
  const value = __ENV[name]
  return value === undefined || value === '' ? fallback : String(value)
}

export function envInt(name, fallback) {
  const raw = envStr(name, '')
  if (raw === '') return fallback
  const value = Number(raw)
  if (!Number.isInteger(value)) throw new Error(`${name} must be an integer, got ${raw}`)
  return value
}

export function envFloat(name, fallback) {
  const raw = envStr(name, '')
  if (raw === '') return fallback
  const value = Number(raw)
  if (!Number.isFinite(value)) throw new Error(`${name} must be a number, got ${raw}`)
  return value
}

export function envBool(name, fallback) {
  const raw = envStr(name, '').toLowerCase()
  if (raw === '') return fallback
  if (['1', 'true', 'yes', 'on'].indexOf(raw) >= 0) return true
  if (['0', 'false', 'no', 'off'].indexOf(raw) >= 0) return false
  throw new Error(`${name} must be on/off, got ${raw}`)
}

export const BASE_URL = envStr('BASE_URL', 'http://127.0.0.1:8701').replace(/\/+$/, '')
export const SCENARIO_LABEL = envStr('SCENARIO_LABEL', '')
const SECRET_VAR = envStr('EDGE_SECRET_ENV', 'ATUS_LT_EDGE_SECRET')
const EDGE_SECRET = __ENV[SECRET_VAR] || ''
if (!EDGE_SECRET && envBool('REQUIRE_EDGE_SECRET', true)) {
  throw new Error(
    `no edge secret: export it as ${SECRET_VAR} (never pass it with -e), ` +
      'or set REQUIRE_EDGE_SECRET=0 for a development API without edge authentication',
  )
}
const REQ_TIMEOUT = envStr('REQ_TIMEOUT', '200s')
const RECORD = envBool('RECORD_REQUESTS', true)

// ------------------------------------------------------------------ metrics
export const M = {
  duration: new Trend('atus_req_duration', true),
  db: new Trend('atus_server_db', true),
  app: new Trend('atus_server_app', true),
  responses: new Counter('atus_responses'),
  ok: new Rate('atus_status_200'),
  shed: new Counter('atus_overloaded'),
  timeouts: new Counter('atus_analysis_timeout'),
  limited: new Counter('atus_rate_limited'),
  serverErrors: new Counter('atus_5xx'),
  transport: new Counter('atus_transport_errors'),
  unexpectedCache: new Counter('atus_unexpected_cache'),
}

// ------------------------------------------------------------------ helpers
/** The SPA's stableStringify: sorted keys, no whitespace (ADR-009 share links). */
export function stableStringify(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value)
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`
  const keys = Object.keys(value)
    .filter((key) => value[key] !== undefined)
    .sort()
  return `{${keys.map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(',')}}`
}

/** mulberry32: a small seeded PRNG, so every run draws the same sequence. */
export function prng(seed) {
  let a = seed >>> 0
  return function next() {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

/** A 32-bit mix of two integers (seed, index) for per-item PRNG seeds. */
export function hash32(a, b) {
  let h = (Math.imul(a >>> 0, 0x9e3779b1) ^ Math.imul((b + 0x7f4a7c15) >>> 0, 0x85ebca6b)) >>> 0
  h = Math.imul(h ^ (h >>> 16), 0x7feb352d) >>> 0
  h = Math.imul(h ^ (h >>> 15), 0x846ca68b) >>> 0
  return (h ^ (h >>> 16)) >>> 0
}

// ------------------------------------------------------------------ client identity
// 198.18.0.0/15 is the benchmarking range (RFC 2544): never a real visitor.
const IP_BASE = 198 * 16777216 + 18 * 65536
const IP_SPACE = 131072
const IP_SLOTS_PER_VU = 256

export function ipFromIndex(index) {
  const n = IP_BASE + (((index % IP_SPACE) + IP_SPACE) % IP_SPACE)
  return [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255].join('.')
}

let ipCharged = 0
let ipSlot = 0
/**
 * The calling VU's client address. Each VU owns 256 addresses; with
 * rotateEvery > 0 it moves to its next address after that many requests
 * (a new "visitor"), so the per-client token buckets (ADR-012: 120 requests
 * and 6 analyses per minute) never dominate a capacity measurement. Rejections
 * still happen and are recorded if a VU outpaces them.
 */
export function vuClientIp(rotateEvery) {
  if (rotateEvery > 0 && ipCharged >= rotateEvery) {
    ipSlot += 1
    ipCharged = 0
  }
  ipCharged += 1
  const vu = exec.vu.idInTest // 0 in setup()
  if (vu === 0) return ipFromIndex(IP_SPACE - 1 - (ipSlot % IP_SLOTS_PER_VU))
  return ipFromIndex((vu - 1) * IP_SLOTS_PER_VU + (ipSlot % IP_SLOTS_PER_VU))
}

/** Iteration number across all VUs, or -1 outside a scenario (setup). */
export function iterationInTest() {
  try {
    return exec.scenario.iterationInTest
  } catch (_error) {
    return -1
  }
}

function header(res, name) {
  const wanted = name.toLowerCase()
  for (const key in res.headers) {
    if (key.toLowerCase() === wanted) return res.headers[key]
  }
  return ''
}

function serverTiming(value) {
  const db = /(?:^|,)\s*db;dur=([0-9.]+)/.exec(value || '')
  const app = /(?:^|,)\s*app;dur=([0-9.]+)/.exec(value || '')
  return { db: db ? Number(db[1]) : null, app: app ? Number(app[1]) : null }
}

function errorCode(res) {
  try {
    const body = JSON.parse(res.body)
    if (body && body.error && typeof body.error.code === 'string') return body.error.code
  } catch (_error) {
    // not an envelope
  }
  return 'non_envelope'
}

/**
 * One API request with the edge headers, metrics and the per-request record.
 *
 * expect: accepted outcomes, as "200" or "422:insufficient_data".
 * expectCache: "hit" / "miss" when the scenario expects one (counted in
 * atus_unexpected_cache otherwise; only for 200 responses).
 */
export function apiRequest(options) {
  const method = options.method || 'GET'
  const ep = options.ep
  const kind = options.kind || ep
  const phase = options.phase || 'main'
  const expect = options.expect || ['200']
  const headers = { Accept: 'application/json', 'Accept-Encoding': 'gzip' }
  if (EDGE_SECRET) headers['X-ATUS-Edge-Auth'] = EDGE_SECRET
  if (options.ip) headers['X-ATUS-Client-IP'] = options.ip
  let payload = null
  if (options.body !== undefined && options.body !== null) {
    payload = typeof options.body === 'string' ? options.body : stableStringify(options.body)
    headers['Content-Type'] = 'application/json'
  }
  const started = Date.now()
  const res = http.request(method, BASE_URL + options.path, payload, {
    headers,
    timeout: REQ_TIMEOUT,
    tags: { name: ep, ep, kind, phase },
  })
  const status = res.status
  const cache = header(res, 'X-Cache')
  const timing = serverTiming(header(res, 'Server-Timing'))
  let code = ''
  if (status === 0) code = `transport_${res.error_code || 'error'}`
  else if (status >= 400) code = errorCode(res)
  const outcome = code ? `${status}:${code}` : String(status)
  const ok = expect.indexOf(outcome) >= 0 || expect.indexOf(String(status)) >= 0
  const tags = {
    ep,
    kind,
    phase,
    status: String(status),
    code: code || 'none',
    cache: cache || 'none',
  }
  M.duration.add(res.timings.duration, tags)
  if (timing.db !== null) M.db.add(timing.db, tags)
  if (timing.app !== null) M.app.add(timing.app, tags)
  M.responses.add(1, tags)
  M.ok.add(status === 200, { ep, phase })
  if (code === 'overloaded') M.shed.add(1, tags)
  if (code === 'analysis_timeout') M.timeouts.add(1, tags)
  if (status === 429) M.limited.add(1, tags)
  if (status >= 500) M.serverErrors.add(1, tags)
  if (status === 0) M.transport.add(1, tags)
  if (options.expectCache && status === 200 && cache !== options.expectCache) {
    M.unexpectedCache.add(1, tags)
  }
  const checks = {}
  checks[`${ep}: ${expect.join('|')}`] = () => ok
  check(res, checks, { ep, phase })
  if (RECORD) {
    console.log(
      JSON.stringify({
        t: 'req',
        scn: SCENARIO_LABEL,
        ph: phase,
        ep,
        kind,
        spec: options.spec || '',
        m: method,
        ts: started,
        st: status,
        code,
        cache,
        dur: res.timings.duration,
        wait: res.timings.waiting,
        db: timing.db,
        app: timing.app,
        bytes: res.body ? res.body.length : 0,
        ra: header(res, 'Retry-After'),
        rid: header(res, 'X-Request-Id'),
        vu: exec.vu.idInTest,
        it: iterationInTest(),
        ok,
      }),
    )
  }
  return { res, status, code, cache, db: timing.db, app: timing.app, ok }
}

/**
 * The release's metadata, read once in setup(): years, presets, measures and
 * the activity codes by level. setup() data is copied to every VU and lands
 * in the summary, so it holds no secret.
 */
export function fetchContext() {
  const ip = vuClientIp(0)
  const meta = apiRequest({ path: '/api/v1/meta', ep: 'meta', phase: 'setup', ip })
  if (meta.status !== 200) throw new Error(`setup: GET /api/v1/meta answered ${meta.status} ${meta.code}`)
  const body = meta.res.json()
  const acts = apiRequest({ path: '/api/v1/activities', ep: 'activities', phase: 'setup', ip })
  if (acts.status !== 200) throw new Error(`setup: GET /api/v1/activities answered ${acts.status} ${acts.code}`)
  const activities = acts.res.json().activities
  const byLevel = { 1: [], 2: [], 3: [] }
  for (const entry of activities) {
    if (byLevel[entry.level]) byLevel[entry.level].push(entry.code)
  }
  const years = body.data.years.slice().sort((a, b) => a - b)
  const detailCodes = []
  const longest = Math.max(byLevel[1].length, byLevel[2].length, byLevel[3].length)
  for (let i = 0; i < longest; i++) {
    for (const level of [1, 2, 3]) {
      if (i < byLevel[level].length) detailCodes.push(byLevel[level][i])
    }
  }
  return {
    dataVersion: header(meta.res, 'X-Data-Version'),
    apiVersion: body.api_version,
    analyticsVersion: body.analytics_version,
    years,
    multiYears: years.filter((year) => year !== 2020),
    latest: years[years.length - 1],
    presets: body.capabilities.activity_presets.slice().sort(),
    measures: body.capabilities.measures.map((measure) => measure.name),
    weightSchemes: body.capabilities.weight_schemes.map((scheme) => scheme.name),
    codes1: byLevel[1],
    codes2: byLevel[2],
    codes3: byLevel[3],
    detailCodes,
  }
}

// ------------------------------------------------------------------ options
function parseStages(text) {
  // "30s:1,60s:2,60s:4" -> ramping-vus stages
  if (!text) throw new Error('EXECUTOR=ramp needs STAGES, e.g. "30s:1,60s:2,60s:4"')
  return text.split(',').map((item) => {
    const [duration, target] = item.trim().split(':')
    return { duration, target: Number(target) }
  })
}

/**
 * Options from the environment, with per-scenario defaults:
 *   EXECUTOR   iterations (shared-iterations: VUS, ITERATIONS) | per-vu
 *              (per-vu-iterations) | duration (constant-vus: VUS, DURATION) |
 *              arrival (constant-arrival-rate: RATE per TIME_UNIT, DURATION,
 *              PRE_VUS, MAX_VUS) | ramp (ramping-vus: STAGES)
 *   GRACEFUL_STOP, MAX_DURATION, SETUP_TIMEOUT
 * `endpoints` become always-true thresholds, which only make k6 report a
 * per-endpoint submetric in the summary.
 */
export function buildOptions(defaults) {
  const executor = envStr('EXECUTOR', defaults.executor || 'iterations')
  const vus = envInt('VUS', defaults.vus || 1)
  const scenario = { exec: 'default', gracefulStop: envStr('GRACEFUL_STOP', '180s') }
  if (executor === 'iterations') {
    Object.assign(scenario, {
      executor: 'shared-iterations',
      vus,
      iterations: envInt('ITERATIONS', defaults.iterations || vus),
      maxDuration: envStr('MAX_DURATION', '6h'),
    })
  } else if (executor === 'per-vu') {
    Object.assign(scenario, {
      executor: 'per-vu-iterations',
      vus,
      iterations: envInt('ITERATIONS', defaults.iterations || 1),
      maxDuration: envStr('MAX_DURATION', '6h'),
    })
  } else if (executor === 'duration') {
    Object.assign(scenario, {
      executor: 'constant-vus',
      vus,
      duration: envStr('DURATION', defaults.duration || '60s'),
    })
  } else if (executor === 'arrival') {
    Object.assign(scenario, {
      executor: 'constant-arrival-rate',
      rate: envInt('RATE', defaults.rate || 1),
      timeUnit: envStr('TIME_UNIT', defaults.timeUnit || '1m'),
      duration: envStr('DURATION', defaults.duration || '10m'),
      preAllocatedVUs: envInt('PRE_VUS', Math.max(vus, 2)),
      maxVUs: envInt('MAX_VUS', Math.max(vus * 4, 20)),
    })
  } else if (executor === 'ramp') {
    Object.assign(scenario, {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: parseStages(envStr('STAGES', defaults.stages || '')),
      gracefulRampDown: envStr('GRACEFUL_STOP', '180s'),
    })
  } else {
    throw new Error(`EXECUTOR must be iterations|per-vu|duration|arrival|ramp, got ${executor}`)
  }
  const thresholds = {}
  for (const ep of defaults.endpoints || []) {
    thresholds[`atus_req_duration{ep:${ep},phase:main}`] = ['max>=0']
  }
  return {
    scenarios: { main: scenario },
    thresholds,
    setupTimeout: envStr('SETUP_TIMEOUT', '45m'),
    teardownTimeout: envStr('TEARDOWN_TIMEOUT', '60s'),
    summaryTrendStats: ['count', 'min', 'med', 'avg', 'p(90)', 'p(95)', 'p(99)', 'max'],
    discardResponseBodies: false,
    userAgent: 'atus-loadtest/1 (k6)',
    noConnectionReuse: envBool('NO_CONNECTION_REUSE', false),
  }
}

// ------------------------------------------------------------------ summary
function fmt(value) {
  return value === undefined || value === null ? '-' : Number(value).toFixed(1)
}

/**
 * handleSummary: the machine-readable summary goes to SUMMARY_JSON (run.py
 * sets it; default summary-<name>.json), and a short text table to stdout.
 * setup_data is dropped (it is metadata only, but large).
 */
export function makeHandleSummary(name) {
  return function handleSummary(data) {
    delete data.setup_data
    const out = {}
    out[envStr('SUMMARY_JSON', `summary-${name}.json`)] = JSON.stringify(data, null, 1)
    // A dry run (run.py --dry-run) is functional only: no latency on stdout.
    const dry = envBool('DRY_RUN', false)
    const lines = [`== ${name}${SCENARIO_LABEL ? ` (${SCENARIO_LABEL})` : ''}${dry ? ' [dry run]' : ''}`]
    for (const key of Object.keys(data.metrics).sort()) {
      if (key.indexOf('atus_req_duration{') !== 0) continue
      const v = data.metrics[key].values
      lines.push(
        dry
          ? `${key}: n=${v.count}`
          : `${key}: n=${v.count} p50=${fmt(v.med)} p95=${fmt(v['p(95)'])} ` +
              `p99=${fmt(v['p(99)'])} max=${fmt(v.max)} ms`,
      )
    }
    for (const key of ['atus_responses', 'atus_overloaded', 'atus_analysis_timeout',
      'atus_rate_limited', 'atus_5xx', 'atus_transport_errors', 'atus_unexpected_cache']) {
      if (data.metrics[key]) lines.push(`${key}: ${data.metrics[key].values.count}`)
    }
    const checks = data.metrics.checks
    if (checks) lines.push(`checks: ${checks.values.passes} passed, ${checks.values.fails} failed`)
    out.stdout = `${lines.join('\n')}\n`
    return out
  }
}
