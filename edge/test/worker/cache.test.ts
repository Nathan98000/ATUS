/**
 * The edge result cache, tier L1 (src/cache.ts; ADR-015): key format,
 * byte-exact replay with `X-Cache: edge`, every condition of the store gate,
 * version scoping, and KV failures that must never fail a request.
 */

import { env } from 'cloudflare:workers'
import { beforeEach, describe, expect, it } from 'vitest'
import { KV_TTL_SECONDS } from '../../src/constants'
import {
  apiJson,
  callWorker,
  expectSecurityHeaders,
  governorStub,
  mockOrigin,
  readEnvelope,
  resetEdgeState,
} from './helpers'

beforeEach(resetEdgeState)

const DATA_VERSION = '0325:serving-20361020T000000Z-0a1b2c3d:0123456789ab'
const RELEASE = { DATA_VERSION, ANALYTICS_VERSION: '0.2' }
const PATH = '/api/v1/analysis/estimate'
const BODY = '{"activity":{"preset":"leisure"},"years":[2024]}'
// Computed independently (Python hashlib):
//   scope = sha256("1|v1|0.2|<DATA_VERSION>|1")[:16]
//   k     = sha256("/api/v1/analysis/estimate?\n" + BODY)
const SCOPE = '10fe49ed95dd1929'
const SCOPE_EPOCH_2 = '22fd56601c872c54'
const K = 'bc90fd68322385f99eb564468bc27e0e3a91011fa64a1178d4dbe4422f05f1e3'
const K_QUERY = '155ba0f38cc68b4f4a62f48a1579db65a2085285b6d831114a63a5607f59b349'
const KEY = `r1/${SCOPE}/${K}`
const ANALYSIS_KEY = 'f'.repeat(64)
/** Formatting a JSON round trip would change: the replay must keep it. */
const RESULT = '{"estimate":{"value":1.2345678901234567e-7, "unit":"minutes"},"x":1e400}'

const GATE_HEADERS = {
  'x-cache': 'miss',
  'x-data-version': DATA_VERSION,
  'x-analytics-version': '0.2',
  'x-response-format': '1',
  'x-analysis-key': ANALYSIS_KEY,
  'server-timing': 'db;dur=3, app;dur=1',
}

function post(body = BODY, path = PATH): [string, RequestInit] {
  return [path, { method: 'POST', headers: { 'content-type': 'application/json' }, body }]
}

async function storedKeys(): Promise<string[]> {
  return (await env.RESULT_CACHE.list()).keys.map((key) => key.name)
}

interface KvCall {
  op: 'get' | 'put'
  key: string
  options?: unknown
}

/** A KV binding that records calls, optionally failing, else delegating to the real one. */
function recordingKv(fail: { get?: boolean; put?: boolean } = {}): {
  kv: KVNamespace
  calls: KvCall[]
} {
  const calls: KvCall[] = []
  const real = env.RESULT_CACHE
  const kv = {
    async getWithMetadata(key: string, options: unknown) {
      calls.push({ op: 'get', key })
      if (fail.get) throw new Error('KV GET failed: 429 Too Many Requests')
      return real.getWithMetadata(key, options as { type: 'arrayBuffer' })
    },
    async put(key: string, value: ArrayBuffer, options: KVNamespacePutOptions) {
      calls.push({ op: 'put', key, options })
      if (fail.put) throw new Error('KV PUT failed: 429 Too Many Requests')
      return real.put(key, value, options)
    },
  }
  return { kv: kv as unknown as KVNamespace, calls }
}

describe('miss, store, then edge hit', () => {
  it('stores the exact origin bytes under r1/<scope>/<k> and replays them with X-Cache: edge', async () => {
    const calls = mockOrigin(() => apiJson(RESULT, 200, GATE_HEADERS))
    const first = await callWorker(...post(), RELEASE)
    expect(first.status).toBe(200)
    expect(first.headers.get('x-cache')).toBe('miss')
    expect(await first.text()).toBe(RESULT)
    expect(calls).toHaveLength(1)

    const stored = await env.RESULT_CACHE.getWithMetadata(KEY, 'arrayBuffer')
    expect(new TextDecoder().decode(stored.value!)).toBe(RESULT)
    expect(stored.metadata).toEqual({
      content_type: 'application/json',
      data_version: DATA_VERSION,
      analytics_version: '0.2',
      response_format: '1',
      analysis_key: ANALYSIS_KEY,
      stored_at: expect.stringMatching(/^\d{4}-\d\d-\d\dT/),
    })

    const second = await callWorker(...post(), RELEASE)
    expect(calls).toHaveLength(1)
    expect(second.status).toBe(200)
    expect(await second.text()).toBe(RESULT)
    expect(second.headers.get('x-cache')).toBe('edge')
    expect(second.headers.get('content-type')).toBe('application/json')
    expect(second.headers.get('x-data-version')).toBe(DATA_VERSION)
    expect(second.headers.get('x-analytics-version')).toBe('0.2')
    expect(second.headers.get('x-response-format')).toBe('1')
    expect(second.headers.get('x-analysis-key')).toBe(ANALYSIS_KEY)
    expect(second.headers.get('x-request-id')).toMatch(/^[0-9a-f]{32}$/)
    expect(second.headers.get('server-timing')).toBeNull()
    expectSecurityHeaders(second)

    // The hit never reached the governor: only the first (computed) analysis counts.
    const report = await governorStub().budget()
    expect(report.analyses.admitted).toBe(1)
  })

  it('stores an origin X-Cache: hit (the API served it from its own cache)', async () => {
    mockOrigin(() => apiJson(RESULT, 200, { ...GATE_HEADERS, 'x-cache': 'hit' }))
    await callWorker(...post(), RELEASE)
    expect(await storedKeys()).toEqual([KEY])
  })

  it('keys on the exact bytes: the same spec serialized differently misses', async () => {
    const calls = mockOrigin(() => apiJson(RESULT, 200, GATE_HEADERS))
    await callWorker(...post(), RELEASE)
    await callWorker(...post('{"years":[2024],"activity":{"preset":"leisure"}}'), RELEASE)
    await callWorker(...post(`${BODY} `), RELEASE)
    expect(calls).toHaveLength(3)
    expect(await storedKeys()).toHaveLength(3)
  })

  it('includes the raw query string in the key', async () => {
    mockOrigin(() => apiJson(RESULT, 200, GATE_HEADERS))
    await callWorker(...post(BODY, `${PATH}?scenario=x`), RELEASE)
    expect(await storedKeys()).toEqual([`r1/${SCOPE}/${K_QUERY}`])
  })

  it('misses once any release var changes (here a manual EPOCH bump)', async () => {
    const calls = mockOrigin(() => apiJson(RESULT, 200, GATE_HEADERS))
    await callWorker(...post(), RELEASE)
    const bumped = await callWorker(...post(), { ...RELEASE, EPOCH: '2' })
    expect(bumped.headers.get('x-cache')).toBe('miss')
    expect(calls).toHaveLength(2)
    expect((await storedKeys()).sort()).toEqual([KEY, `r1/${SCOPE_EPOCH_2}/${K}`].sort())
  })

  it('misses when the deployment moved to another data version, and does not store the old one', async () => {
    const calls = mockOrigin(() => apiJson(RESULT, 200, GATE_HEADERS))
    await callWorker(...post(), RELEASE)
    const next = {
      ...RELEASE,
      DATA_VERSION: '0326:serving-20370101T000000Z-0a1b2c3d:abcdefabcdef',
    }
    await callWorker(...post(), next)
    expect(calls).toHaveLength(2)
    // The origin still reports the old version (mid-switch): the gate refuses it.
    expect(await storedKeys()).toEqual([KEY])
  })

  it('treats an entry whose metadata does not match the deployment as a miss', async () => {
    await env.RESULT_CACHE.put(KEY, new TextEncoder().encode('{"stale":true}'), {
      metadata: {
        content_type: 'application/json',
        data_version: 'something-else',
        analytics_version: '0.2',
        response_format: '1',
        analysis_key: null,
        stored_at: '2036-01-01T00:00:00.000Z',
      },
    })
    const calls = mockOrigin(() => apiJson(RESULT, 200, GATE_HEADERS))
    const response = await callWorker(...post(), RELEASE)
    expect(await response.text()).toBe(RESULT)
    expect(calls).toHaveLength(1)
  })

  it('stores for 30 days', async () => {
    mockOrigin(() => apiJson(RESULT, 200, GATE_HEADERS))
    const { kv, calls } = recordingKv()
    await callWorker(...post(), { ...RELEASE, RESULT_CACHE: kv })
    expect(calls.map((call) => call.op)).toEqual(['get', 'put'])
    expect(calls[1]!.key).toBe(KEY)
    expect(calls[1]!.options).toMatchObject({ expirationTtl: KV_TTL_SECONDS })
    expect(KV_TTL_SECONDS).toBe(2_592_000)
  })
})

describe('the store gate', () => {
  const big = (bytes: number): string => `{"x":"${'a'.repeat(bytes - 8)}"}`

  it.each([
    ['X-Cache: bypass (computed during a data reload)', { 'x-cache': 'bypass' }],
    ['X-Cache: join', { 'x-cache': 'join' }],
    ['no X-Cache', { 'x-cache': '' }],
    ['another X-Data-Version', { 'x-data-version': 'other' }],
    ['no X-Data-Version', { 'x-data-version': '' }],
    ['another X-Analytics-Version', { 'x-analytics-version': '0.3' }],
    ['another X-Response-Format', { 'x-response-format': '2' }],
    ['no X-Response-Format', { 'x-response-format': '' }],
  ])('does not store a response with %s', async (_label, override) => {
    const headers: Record<string, string> = { ...GATE_HEADERS }
    for (const [name, value] of Object.entries(override)) {
      if (value === '') delete headers[name]
      else headers[name] = value
    }
    mockOrigin(() => apiJson(RESULT, 200, headers))
    const response = await callWorker(...post(), RELEASE)
    expect(await response.text()).toBe(RESULT)
    expect(await storedKeys()).toEqual([])
  })

  it('does not store a 2xx other than 200', async () => {
    mockOrigin(() => apiJson(RESULT, 201, GATE_HEADERS))
    expect((await callWorker(...post(), RELEASE)).status).toBe(201)
    expect(await storedKeys()).toEqual([])
  })

  it.each([
    [422, '{"error":{"code":"validation_error","message":"x","details":null}}'],
    [503, '{"error":{"code":"analysis_timeout","message":"x","details":null}}'],
    [429, '{"error":{"code":"rate_limited","message":"x","details":null}}'],
  ])('never stores an error (%i)', async (status, body) => {
    mockOrigin(() => apiJson(body, status, GATE_HEADERS))
    const response = await callWorker(...post(), RELEASE)
    expect(response.status).toBe(status)
    expect(await storedKeys()).toEqual([])
  })

  it('stores a body of exactly 16 KiB', async () => {
    const body = big(16 * 1024)
    mockOrigin(() => apiJson(body, 200, GATE_HEADERS))
    const response = await callWorker(...post(), RELEASE)
    expect(await response.text()).toBe(body)
    expect(await storedKeys()).toEqual([KEY])
  })

  it('relays but does not store a body over 16 KiB', async () => {
    const body = big(16 * 1024 + 1)
    mockOrigin(() => apiJson(body, 200, GATE_HEADERS))
    const response = await callWorker(...post(), RELEASE)
    expect(await response.text()).toBe(body)
    expect(await storedKeys()).toEqual([])
  })

  it('relays a large body byte for byte when it streams in many chunks', async () => {
    const body = big(100_000)
    mockOrigin(() => {
      const bytes = new TextEncoder().encode(body)
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          for (let i = 0; i < bytes.length; i += 1000)
            controller.enqueue(bytes.slice(i, i + 1000))
          controller.close()
        },
      })
      return new Response(stream, {
        headers: { 'content-type': 'application/json', ...GATE_HEADERS },
      })
    })
    const response = await callWorker(...post(), RELEASE)
    expect(await response.text()).toBe(body)
    expect(await storedKeys()).toEqual([])
  })

  it('answers 502 and stores nothing when the body breaks before it is complete', async () => {
    mockOrigin(() => {
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new TextEncoder().encode('{"estimate":'))
          controller.error(new Error('connection reset'))
        },
      })
      return new Response(stream, {
        headers: { 'content-type': 'application/json', ...GATE_HEADERS },
      })
    })
    const response = await callWorker(...post(), RELEASE)
    expect(response.status).toBe(502)
    expect((await readEnvelope(response)).code).toBe('upstream_unavailable')
    expect(await storedKeys()).toEqual([])
  })
})

describe('cache off and KV failures', () => {
  it('neither reads nor writes KV without release vars', async () => {
    mockOrigin(() => apiJson(RESULT, 200, GATE_HEADERS))
    const { kv, calls } = recordingKv()
    const response = await callWorker(...post(), { RESULT_CACHE: kv, DATA_VERSION: '' })
    expect(response.status).toBe(200)
    expect(calls).toEqual([])
  })

  it('never touches KV for GET routes', async () => {
    mockOrigin(() => apiJson('{"api_version":"v1"}', 200, GATE_HEADERS))
    const { kv, calls } = recordingKv()
    await callWorker('/api/v1/meta', undefined, { ...RELEASE, RESULT_CACHE: kv })
    expect(calls).toEqual([])
  })

  it('treats a failed KV read as a miss and ignores a failed write', async () => {
    const origin = mockOrigin(() => apiJson(RESULT, 200, GATE_HEADERS))
    const { kv, calls } = recordingKv({ get: true, put: true })
    const response = await callWorker(...post(), { ...RELEASE, RESULT_CACHE: kv })
    expect(response.status).toBe(200)
    expect(await response.text()).toBe(RESULT)
    expect(origin).toHaveLength(1)
    expect(calls.map((call) => call.op)).toEqual(['get', 'put'])
  })

  it('serves a hit while the origin is unconfigured or the governor is down', async () => {
    mockOrigin(() => apiJson(RESULT, 200, GATE_HEADERS))
    await callWorker(...post(), RELEASE)
    const brokenGovernor = {
      idFromName: () => ({}),
      get: () => ({
        admit: () => Promise.reject(new Error('Durable Object is overloaded')),
      }),
    } as unknown as Env['GOVERNOR']
    const response = await callWorker(...post(), {
      ...RELEASE,
      RENDER_ORIGIN: '',
      GOVERNOR: brokenGovernor,
    })
    expect(response.status).toBe(200)
    expect(response.headers.get('x-cache')).toBe('edge')
    expect(await response.text()).toBe(RESULT)
  })
})
