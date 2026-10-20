/**
 * Origin-call resilience (ADR-011 step 3.6 and the P4 graft): no Worker
 * timeout, exactly one retry and only on a connection failure before
 * response headers, and the per-isolate circuit breaker.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { BREAKER_OPEN_MS } from '../../src/constants'
import { CircuitBreaker } from '../../src/upstream'
import {
  apiJson,
  callWorker,
  expectSecurityHeaders,
  mockOrigin,
  readEnvelope,
  resetEdgeState,
} from './helpers'

beforeEach(resetEdgeState)

const NETWORK_ERROR = (): never => {
  throw new TypeError('Network connection lost.')
}

/** Freeze `Date.now()` at a controllable instant. */
function controlClock(start = 1_700_000_000_000): { advance(ms: number): void } {
  let now = start
  vi.spyOn(Date, 'now').mockImplementation(() => now)
  return {
    advance(ms: number) {
      now += ms
    },
  }
}

describe('single retry', () => {
  it('retries once after a connection failure and returns the second answer', async () => {
    let attempt = 0
    const calls = mockOrigin(() => {
      attempt += 1
      return attempt === 1 ? NETWORK_ERROR() : apiJson('{"ok":true}')
    })
    const response = await callWorker('/api/v1/meta')
    expect(response.status).toBe(200)
    expect(await response.text()).toBe('{"ok":true}')
    expect(calls).toHaveLength(2)
    // Same request, same request ID, both times.
    expect(calls[1]!.headers.get('x-request-id')).toBe(calls[0]!.headers.get('x-request-id'))
  })

  it('replays the exact POST body on the retry', async () => {
    let attempt = 0
    const calls = mockOrigin(() => {
      attempt += 1
      return attempt === 1 ? NETWORK_ERROR() : apiJson('{"ok":true}')
    })
    const body = '{"analysis":"trend","years":[2003,2025]}'
    await callWorker('/api/v1/analysis/trend', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body,
    })
    expect(calls.map((call) => call.body)).toEqual([body, body])
  })

  it('gives up after exactly two attempts with 502 upstream_unavailable', async () => {
    const calls = mockOrigin(NETWORK_ERROR)
    const response = await callWorker('/api/v1/meta')
    expect(calls).toHaveLength(2)
    expect(response.status).toBe(502)
    expect(response.headers.get('retry-after')).toBe('10')
    expectSecurityHeaders(response)
    expect((await readEnvelope(response)).code).toBe('upstream_unavailable')
  })

  it.each([
    [
      'an API 503 envelope',
      () => apiJson('{"error":{"code":"overloaded","message":"x","details":null}}', 503),
    ],
    [
      'Render hibernate',
      () =>
        new Response('', {
          status: 503,
          headers: { 'x-render-routing': 'dynamic-hibernate-error-503' },
        }),
    ],
    ['a Cloudflare 524', () => new Response('<html>', { status: 524 })],
    [
      'a 500',
      () => apiJson('{"error":{"code":"internal_error","message":"x","details":null}}', 500),
    ],
    ['a 502 page', () => new Response('Bad Gateway', { status: 502 })],
  ])('never retries once response headers arrived: %s', async (_, make) => {
    const calls = mockOrigin(make)
    await callWorker('/api/v1/analysis/estimate', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{}',
    })
    expect(calls).toHaveLength(1)
  })
})

describe('no Worker-imposed timeout', () => {
  it('passes no abort signal to the origin call', async () => {
    const calls = mockOrigin(() => apiJson('{}'))
    await callWorker('/api/v1/analysis/estimate', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{}',
    })
    expect(calls[0]!.init.signal ?? null).toBeNull()
  })

  it('waits for an origin that answers after a long delay', async () => {
    const body = '{"result":"slow but complete"}'
    mockOrigin(async () => {
      await new Promise((resolve) => setTimeout(resolve, 3_000))
      return apiJson(body)
    })
    const response = await callWorker('/api/v1/analysis/estimate', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{}',
    })
    expect(response.status).toBe(200)
    expect(await response.text()).toBe(body)
  }, 15_000)
})

describe('circuit breaker', () => {
  it('opens after two consecutive connection failures and skips the origin for 30 s', async () => {
    const clock = controlClock()
    const calls = mockOrigin(NETWORK_ERROR)
    const failed = await callWorker('/api/v1/meta')
    expect(failed.status).toBe(502)
    expect(calls).toHaveLength(2)

    const skipped = await callWorker('/api/v1/analysis/estimate', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{}',
    })
    expect(calls).toHaveLength(2)
    expect(skipped.status).toBe(503)
    expect(skipped.headers.get('retry-after')).toBe('30')
    expectSecurityHeaders(skipped)
    const error = await readEnvelope(skipped)
    expect(error.code).toBe('upstream_unavailable')
    expect(error.details).toEqual({ retry_after_seconds: 30 })

    clock.advance(12_500)
    const later = await callWorker('/api/health')
    expect(later.status).toBe(503)
    expect(later.headers.get('retry-after')).toBe('18')
    expect(calls).toHaveLength(2)
  })

  it('tries the origin again once the 30 s are over, and closes on any response', async () => {
    const clock = controlClock()
    let up = false
    const calls = mockOrigin(() => (up ? apiJson('{"ok":true}') : NETWORK_ERROR()))
    await callWorker('/api/v1/meta')
    expect((await callWorker('/api/v1/meta')).status).toBe(503)

    clock.advance(BREAKER_OPEN_MS)
    up = true
    const recovered = await callWorker('/api/v1/meta')
    expect(recovered.status).toBe(200)
    expect(calls).toHaveLength(3)
    expect((await callWorker('/api/v1/meta')).status).toBe(200)
    expect(calls).toHaveLength(4)
  })

  it('re-opens when the trial after 30 s fails again', async () => {
    const clock = controlClock()
    const calls = mockOrigin(NETWORK_ERROR)
    await callWorker('/api/v1/meta')
    clock.advance(BREAKER_OPEN_MS)
    expect((await callWorker('/api/v1/meta')).status).toBe(502)
    expect(calls).toHaveLength(4)
    expect((await callWorker('/api/v1/meta')).status).toBe(503)
    expect(calls).toHaveLength(4)
  })

  it('counts only consecutive failures: a response in between resets it', async () => {
    let attempt = 0
    // fail, ok, fail, ok, ...: never two failures in a row.
    const calls = mockOrigin(() => {
      attempt += 1
      return attempt % 2 === 1 ? NETWORK_ERROR() : apiJson('{}')
    })
    for (let i = 0; i < 4; i += 1) expect((await callWorker('/api/v1/meta')).status).toBe(200)
    expect(calls).toHaveLength(8)
  })

  it('treats an HTTP error response as a working connection', async () => {
    const calls = mockOrigin(() => new Response('<html>', { status: 522 }))
    for (let i = 0; i < 3; i += 1) expect((await callWorker('/api/v1/meta')).status).toBe(502)
    expect(calls).toHaveLength(3)
  })

  it('does not affect routes the Worker answers itself', async () => {
    mockOrigin(NETWORK_ERROR)
    await callWorker('/api/v1/meta')
    expect((await callWorker('/api/_edge/health')).status).toBe(200)
    expect((await callWorker('/api/nope')).status).toBe(404)
  })

  it('CircuitBreaker unit: threshold, window and reset', () => {
    const breaker = new CircuitBreaker(2, 30_000)
    expect(breaker.remainingOpenMs(0)).toBe(0)
    breaker.recordFailure(1_000)
    expect(breaker.remainingOpenMs(1_000)).toBe(0)
    breaker.recordFailure(2_000)
    expect(breaker.remainingOpenMs(2_000)).toBe(30_000)
    expect(breaker.remainingOpenMs(31_999)).toBe(1)
    expect(breaker.remainingOpenMs(32_000)).toBe(0)
    breaker.recordSuccess()
    breaker.recordFailure(40_000)
    expect(breaker.remainingOpenMs(40_000)).toBe(0)
  })
})
