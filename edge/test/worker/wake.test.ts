/** GET /api/v1/wake: user-intent prewake (ADR-011 "Prewake"). */

import { waitOnExecutionContext } from 'cloudflare:test'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { WAKE_INTERVAL_MS, WAKE_RETRY_MS } from '../../src/constants'
import {
  EDGE_SECRET,
  apiJson,
  callWorker,
  callWorkerNoWait,
  expectSecurityHeaders,
  mockOrigin,
  resetEdgeState,
} from './helpers'

beforeEach(resetEdgeState)

const HEALTH = 'https://origin.invalid/health'

function controlClock(start = 1_700_000_000_000): { advance(ms: number): void } {
  let now = start
  vi.spyOn(Date, 'now').mockImplementation(() => now)
  return {
    advance(ms: number) {
      now += ms
    },
  }
}

describe('wake', () => {
  it('reports a warm origin and calls only origin /health', async () => {
    const calls = mockOrigin(() => apiJson('{"status":"ok"}'))
    const response = await callWorker('/api/v1/wake')
    expect(response.status).toBe(200)
    expectSecurityHeaders(response)
    expect(await response.json()).toEqual({ origin: 'warm', wake: 'sent' })
    expect(calls).toHaveLength(1)
    expect(calls[0]!.url).toBe(HEALTH)
    expect(calls[0]!.method).toBe('GET')
    expect(calls[0]!.body).toBeNull()
    expect(calls[0]!.headers.get('x-atus-edge-auth')).toBe(EDGE_SECRET)
  })

  it('answers "waking" after 1.5 s and keeps the origin call alive through waitUntil', async () => {
    let answered = false
    const calls = mockOrigin(async () => {
      await new Promise((resolve) => setTimeout(resolve, 3_000))
      answered = true
      return apiJson('{"status":"ok"}')
    })
    const { response, ctx } = await callWorkerNoWait('/api/v1/wake')
    expect(await response.json()).toEqual({ origin: 'waking', wake: 'sent' })
    expect(answered).toBe(false)

    await waitOnExecutionContext(ctx)
    expect(answered).toBe(true)
    // The completed wake is remembered: the next call reports it without a new origin call.
    const again = await callWorker('/api/v1/wake')
    expect(await again.json()).toEqual({ origin: 'warm', wake: 'recent' })
    expect(calls).toHaveLength(1)
  }, 15_000)

  it('reports "waking" when origin /health answers with Render\'s hibernate 503', async () => {
    mockOrigin(
      () =>
        new Response('', {
          status: 503,
          headers: { 'x-render-routing': 'dynamic-hibernate-error-503' },
        }),
    )
    const response = await callWorker('/api/v1/wake')
    expect(await response.json()).toEqual({ origin: 'waking', wake: 'sent' })
  })

  it('reports "unknown" when the origin cannot be reached', async () => {
    const calls = mockOrigin(() => {
      throw new TypeError('Network connection lost.')
    })
    const response = await callWorker('/api/v1/wake')
    expect(response.status).toBe(200)
    expect(await response.json()).toEqual({ origin: 'unknown', wake: 'sent' })
    expect(calls).toHaveLength(2)
  })

  it('sends at most one wake per isolate per 10 minutes', async () => {
    const clock = controlClock()
    const calls = mockOrigin(() => apiJson('{"status":"ok"}'))
    await callWorker('/api/v1/wake')
    clock.advance(WAKE_INTERVAL_MS - 1)
    const recent = await callWorker('/api/v1/wake')
    expect(await recent.json()).toEqual({ origin: 'warm', wake: 'recent' })
    expect(calls).toHaveLength(1)

    clock.advance(1)
    const fresh = await callWorker('/api/v1/wake')
    expect(await fresh.json()).toEqual({ origin: 'warm', wake: 'sent' })
    expect(calls).toHaveLength(2)
  })

  it('probes an origin last seen waking again after a minute, a warm one after 10 minutes', async () => {
    // Render is up about a minute after its hibernate answer; remembering
    // "waking" for 10 minutes showed the cold-start notice on a warm service.
    const clock = controlClock()
    let warm = false
    const calls = mockOrigin(() =>
      warm
        ? apiJson('{"status":"ok"}')
        : new Response('', {
            status: 503,
            headers: { 'x-render-routing': 'dynamic-hibernate-error-503' },
          }),
    )
    const wake = async (): Promise<unknown> => (await callWorker('/api/v1/wake')).json()
    expect(await wake()).toEqual({ origin: 'waking', wake: 'sent' })
    warm = true
    clock.advance(WAKE_RETRY_MS - 1)
    expect(await wake()).toEqual({ origin: 'waking', wake: 'recent' })
    expect(calls).toHaveLength(1)
    clock.advance(1)
    expect(await wake()).toEqual({ origin: 'warm', wake: 'sent' })
    expect(calls).toHaveLength(2)
    clock.advance(WAKE_INTERVAL_MS - 1)
    expect(await wake()).toEqual({ origin: 'warm', wake: 'recent' })
    expect(calls).toHaveLength(2)
  })

  it('probes again after a minute when the last probe never finished (cut off with its request)', async () => {
    const clock = controlClock()
    const calls = mockOrigin(() => new Promise<Response>(() => {}))
    const { response } = await callWorkerNoWait('/api/v1/wake')
    expect(await response.json()).toEqual({ origin: 'waking', wake: 'sent' })
    clock.advance(WAKE_RETRY_MS)
    mockOrigin(() => apiJson('{"status":"ok"}'))
    expect(await (await callWorker('/api/v1/wake')).json()).toEqual({
      origin: 'warm',
      wake: 'sent',
    })
    expect(calls).toHaveLength(1)
  })

  it('sends one origin call for concurrent wakes', async () => {
    const calls = mockOrigin(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50))
      return apiJson('{"status":"ok"}')
    })
    const results = await Promise.all([
      callWorker('/api/v1/wake'),
      callWorker('/api/v1/wake'),
      callWorker('/api/v1/wake'),
    ])
    expect(calls).toHaveLength(1)
    const bodies = await Promise.all(results.map((response) => response.json()))
    expect(bodies.filter((body) => (body as { wake: string }).wake === 'sent')).toHaveLength(1)
  })

  it('PREWAKE=off disables the origin call', async () => {
    const calls = mockOrigin(() => apiJson('{"status":"ok"}'))
    const response = await callWorker('/api/v1/wake', undefined, { PREWAKE: 'off' })
    expect(response.status).toBe(200)
    expect(await response.json()).toEqual({ origin: 'unknown', wake: 'off' })
    expect(calls).toHaveLength(0)
  })

  it('skips the origin while the circuit breaker is open, without using up the window', async () => {
    const clock = controlClock()
    let up = false
    const calls = mockOrigin(() => {
      if (!up) throw new TypeError('Network connection lost.')
      return apiJson('{"status":"ok"}')
    })
    await callWorker('/api/v1/meta') // two connection failures: breaker open
    expect(calls).toHaveLength(2)
    const skipped = await callWorker('/api/v1/wake')
    expect(await skipped.json()).toEqual({ origin: 'unknown', wake: 'circuit_open' })
    expect(calls).toHaveLength(2)

    clock.advance(30_000)
    up = true
    const sent = await callWorker('/api/v1/wake')
    expect(await sent.json()).toEqual({ origin: 'warm', wake: 'sent' })
    expect(calls.map((call) => call.url).slice(2)).toEqual([HEALTH])
  })

  it('ignores the query string and any client headers beyond the allowlist', async () => {
    const calls = mockOrigin(() => apiJson('{"status":"ok"}'))
    await callWorker('/api/v1/wake?target=/api/v1/analysis/estimate', {
      headers: {
        'X-ATUS-Edge-Auth': 'forged',
        Cookie: 'a=b',
        'CF-Connecting-IP': '203.0.113.9',
      },
    })
    expect(calls[0]!.url).toBe(HEALTH)
    expect(calls[0]!.headers.get('x-atus-edge-auth')).toBe(EDGE_SECRET)
    expect(calls[0]!.headers.get('cookie')).toBeNull()
    expect(calls[0]!.headers.get('x-atus-client-ip')).toBe('203.0.113.9')
  })
})
