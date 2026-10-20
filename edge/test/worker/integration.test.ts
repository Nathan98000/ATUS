/**
 * The Worker against a real HTTP origin over loopback sockets (the server in
 * test/global-setup.ts): what actually arrives upstream, byte-exact bodies
 * through real (and gzip-encoded) responses, a long wait for headers, dropped
 * and refused connections.
 */

import { beforeEach, describe, expect, inject, it } from 'vitest'
import { ERROR_BODY, FIXED_BODY } from '../fixtures'
import {
  EDGE_SECRET,
  callWorker,
  expectSecurityHeaders,
  readEnvelope,
  resetEdgeState,
} from './helpers'

const local = { RENDER_ORIGIN: inject('localOrigin') }

beforeEach(resetEdgeState)

interface Echo {
  method: string
  path: string
  query: string
  headers: Record<string, string>
  body: string
}

async function connections(key: string): Promise<number> {
  const response = await fetch(`${inject('localOrigin')}/_count?key=${key}`)
  return ((await response.json()) as { count: number }).count
}

describe('against a local origin', () => {
  it('delivers only the allowlisted headers and the exact body upstream', async () => {
    const body = '{"activity":{"preset":"leisure"},"analysis":"estimate","years":[2024]}'
    const response = await callWorker(
      '/api/v1/analysis/estimate?scenario=echo',
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'CF-Connecting-IP': '203.0.113.7',
          'X-ATUS-Client-IP': '198.51.100.99',
          'X-ATUS-Edge-Auth': 'forged',
          'X-Forwarded-For': '198.51.100.1',
          Forwarded: 'for=198.51.100.3',
          'X-Real-IP': '198.51.100.4',
          Cookie: 'session=stolen',
          Authorization: 'Bearer stolen',
        },
        body,
      },
      local,
    )
    expect(response.status).toBe(200)
    const echo = (await response.json()) as Echo
    expect(echo.method).toBe('POST')
    expect(echo.path).toBe('/api/v1/analysis/estimate')
    expect(echo.body).toBe(body)
    const arrived = echo.headers
    expect(arrived['x-atus-client-ip']).toBe('203.0.113.7')
    expect(arrived['x-atus-edge-auth']).toBe(EDGE_SECRET)
    expect(arrived['user-agent']).toBe('atus-edge/0.1.0')
    expect(arrived['x-request-id']).toMatch(/^[A-Za-z0-9._-]{8,64}$/)
    for (const name of [
      'x-forwarded-for',
      'forwarded',
      'x-real-ip',
      'cookie',
      'authorization',
    ]) {
      expect(arrived[name], name).toBeUndefined()
    }
    // Only transport headers the runtime adds itself appear beyond the allowlist.
    const allowed = [
      'accept',
      'accept-encoding',
      'content-type',
      'if-none-match',
      'user-agent',
      'x-atus-client-ip',
      'x-atus-edge-auth',
      'x-request-id',
    ]
    const transport = ['host', 'content-length', 'connection', 'cf-worker', 'transfer-encoding']
    for (const name of Object.keys(arrived)) {
      expect([...allowed, ...transport], name).toContain(name)
    }
  })

  it('strips the origin platform headers from a real response', async () => {
    const response = await callWorker('/api/v1/meta?scenario=echo', undefined, local)
    for (const name of ['server', 'rndr-id', 'x-render-origin-server', 'set-cookie']) {
      expect(response.headers.get(name), name).toBeNull()
    }
    expectSecurityHeaders(response)
  })

  it('passes an error envelope through byte for byte', async () => {
    const response = await callWorker('/api/v1/meta?scenario=error', undefined, local)
    expect(response.status).toBe(422)
    expect(await response.text()).toBe(ERROR_BODY)
    expect(response.headers.get('server')).toBeNull()
  })

  it.each([
    ['without', undefined],
    ['with', { 'Accept-Encoding': 'gzip, deflate, br' }],
  ])(
    'passes a gzip-encoded JSON body through with identical decoded bytes (%s client Accept-Encoding)',
    async (_, headers) => {
      const response = await callWorker('/api/v1/meta?scenario=gzip', { headers }, local)
      expect(response.status).toBe(200)
      expect(response.headers.get('content-encoding')).toBeNull()
      const bytes = new Uint8Array(await response.arrayBuffer())
      expect([...bytes]).toEqual([...new TextEncoder().encode(FIXED_BODY)])
    },
  )

  it('passes a gzip-encoded error envelope through with identical decoded bytes', async () => {
    const response = await callWorker(
      '/api/v1/meta?scenario=gzip-error',
      { headers: { 'Accept-Encoding': 'gzip' } },
      local,
    )
    expect(response.status).toBe(429)
    expect(response.headers.get('retry-after')).toBe('7')
    expect(await response.text()).toBe(ERROR_BODY)
  })

  it('waits for headers that take seconds to arrive (no Worker timeout)', async () => {
    const response = await callWorker(
      '/api/v1/analysis/estimate?scenario=slow&delay=4000',
      { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' },
      local,
    )
    expect(response.status).toBe(200)
    expect(await response.text()).toBe(FIXED_BODY)
    expect(response.headers.get('x-request-id')).toBe('origin-slow-1')
  }, 20_000)

  it('retries a dropped connection once and returns the answer', async () => {
    const key = `once-${crypto.randomUUID()}`
    const response = await callWorker(
      `/api/v1/meta?scenario=reset-once&key=${key}`,
      undefined,
      local,
    )
    expect(response.status).toBe(200)
    expect(await connections(key)).toBe(2)
  })

  it('stops after two dropped connections with 502 and opens the breaker', async () => {
    const key = `reset-${crypto.randomUUID()}`
    const response = await callWorker(
      `/api/v1/meta?scenario=reset&key=${key}`,
      undefined,
      local,
    )
    expect(response.status).toBe(502)
    expect((await readEnvelope(response)).code).toBe('upstream_unavailable')
    expect(await connections(key)).toBe(2)

    const skipped = await callWorker(`/api/v1/meta?scenario=reset&key=${key}`, undefined, local)
    expect(skipped.status).toBe(503)
    expect(await connections(key)).toBe(2)
  })

  it('treats a refused connection as a connection failure', async () => {
    const closed = { RENDER_ORIGIN: inject('closedOrigin') }
    const response = await callWorker('/api/v1/meta', undefined, closed)
    expect(response.status).toBe(502)
    expect((await readEnvelope(response)).code).toBe('upstream_unavailable')
    expect((await callWorker('/api/v1/meta', undefined, closed)).status).toBe(503)
  })

  it.each(['hibernate', 'html'])('maps the %s scenario to service_waking', async (scenario) => {
    const response = await callWorker(`/api/v1/meta?scenario=${scenario}`, undefined, local)
    expect(response.status).toBe(503)
    expect(response.headers.get('retry-after')).toBe('5')
    expect((await readEnvelope(response)).code).toBe('service_waking')
  })

  it('wakes a warm origin through /health', async () => {
    const response = await callWorker('/api/v1/wake', undefined, local)
    expect(await response.json()).toEqual({ origin: 'warm', wake: 'sent' })
  })

  it('stores a gzip-encoded analysis decoded, then answers it from KV without the origin', async () => {
    const key = `cacheable-${crypto.randomUUID()}`
    const dataVersion = '0325:serving-20361020T000000Z-0a1b2c3d:0123456789ab'
    const release = { ...local, DATA_VERSION: dataVersion, ANALYTICS_VERSION: '0.2' }
    const path = `/api/v1/analysis/estimate?scenario=cacheable&dv=${encodeURIComponent(dataVersion)}&key=${key}`
    const init = (): RequestInit => ({
      method: 'POST',
      headers: { 'content-type': 'application/json', 'accept-encoding': 'gzip' },
      body: '{"years":[2024]}',
    })
    const first = await callWorker(path, init(), release)
    expect(first.status).toBe(200)
    expect(first.headers.get('x-cache')).toBe('miss')
    expect(await first.text()).toBe(FIXED_BODY)

    const second = await callWorker(path, init(), release)
    expect(second.headers.get('x-cache')).toBe('edge')
    expect(second.headers.get('x-data-version')).toBe(dataVersion)
    expect(await second.text()).toBe(FIXED_BODY)
    expect(await connections(key)).toBe(1)
  })

  it('forwards /api/health to origin /health', async () => {
    const response = await callWorker('/api/health', undefined, local)
    expect(((await response.json()) as Echo).path).toBe('/health')
  })
})
