/**
 * The Worker's side of the governor (src/admission.ts): client prefixes and
 * their daily-keyed HMAC, the cost class, and how an origin response is
 * reported back when the analysis settles.
 */

import { describe, expect, it } from 'vitest'
import {
  costClass,
  ipPrefix,
  prefixHmac,
  serverTimingDbMs,
  settleReportFor,
} from '../../src/admission'
import type { Relayed } from '../../src/proxy'

const T = Date.UTC(2026, 9, 20, 12, 0, 0)
const encode = (text: string): Uint8Array => new TextEncoder().encode(text)

describe('ipPrefix', () => {
  it.each([
    ['203.0.113.7', '203.0.113.0/24'],
    ['203.0.113.250', '203.0.113.0/24'],
    ['0.0.0.0', '0.0.0.0/24'],
    ['2001:db8:abcd:12::1', '2001:0db8:abcd::/48'],
    ['2001:DB8:ABCD:0012:0000:0000:0000:0001', '2001:0db8:abcd::/48'],
    ['2001:db8::1', '2001:0db8:0000::/48'],
    ['::1', '0000:0000:0000::/48'],
    ['::', '0000:0000:0000::/48'],
    ['::ffff:198.51.100.23', '198.51.100.0/24'],
    ['::ffff:c633:6417', '198.51.100.0/24'],
    ['64:ff9b::198.51.100.23', '0064:ff9b:0000::/48'],
  ])('%s -> %s', (ip, prefix) => {
    expect(ipPrefix(ip)).toBe(prefix)
  })

  it.each([
    null,
    '',
    '256.1.1.1',
    '1.2.3',
    '1.2.3.4.5',
    '2001:db8::1::2',
    '2001:db8:1:2:3:4:5:6:7',
    '1:2:3:4:5:6:7',
    'g::1',
    'fe80::1%eth0',
    '::ffff:300.1.1.1',
    'localhost',
  ])('%s -> unknown', (ip) => {
    expect(ipPrefix(ip)).toBe('unknown')
  })
})

describe('prefixHmac', () => {
  it('matches HMAC-SHA256(HMAC-SHA256(secret, "atus-ip-prefix|<day>"), prefix)[:32]', async () => {
    // Computed independently with Python's hmac module.
    expect(await prefixHmac('test-ip-hmac-key', T, '203.0.113.0/24')).toBe(
      '6c8524e3410697a76f2119cb98d2f2d2',
    )
  })

  it('is stable within a UTC day and rotates at midnight', async () => {
    const morning = await prefixHmac('k', Date.UTC(2026, 9, 20, 0, 0, 1), '203.0.113.0/24')
    const evening = await prefixHmac('k', Date.UTC(2026, 9, 20, 23, 59, 59), '203.0.113.0/24')
    const nextDay = await prefixHmac('k', Date.UTC(2026, 9, 21, 0, 0, 1), '203.0.113.0/24')
    expect(evening).toBe(morning)
    expect(nextDay).not.toBe(morning)
  })

  it('separates prefixes and secrets and never contains the address', async () => {
    const a = await prefixHmac('k', T, '203.0.113.0/24')
    const b = await prefixHmac('k', T, '203.0.114.0/24')
    const c = await prefixHmac('other', T, '203.0.113.0/24')
    expect(new Set([a, b, c]).size).toBe(3)
    for (const value of [a, b, c]) {
      expect(value).toMatch(/^[0-9a-f]{32}$/)
      expect(value).not.toContain('203')
    }
  })
})

describe('costClass (the API lane rule: len(years) x (2 if compare) >= 8)', () => {
  const years = (n: number): string =>
    JSON.stringify({ years: Array.from({ length: n }, (_, i) => 2003 + i) })

  it.each([
    ['analysis.estimate', 1, 'light'],
    ['analysis.estimate', 7, 'light'],
    ['analysis.estimate', 8, 'heavy'],
    ['analysis.trend', 23, 'heavy'],
    ['analysis.compare', 3, 'light'],
    ['analysis.compare', 4, 'heavy'],
  ])('%s with %i years is %s', (route, n, expected) => {
    expect(costClass(route, encode(years(n)))).toBe(expected)
  })

  it.each([
    ['not JSON', 'nope'],
    ['no years', '{"activity":{"preset":"leisure"}}'],
    ['years not a list', '{"years":2024}'],
    ['a JSON array', '[2024]'],
    ['null', 'null'],
  ])('is heavy when the body has %s', (_label, body) => {
    expect(costClass('analysis.estimate', encode(body))).toBe('heavy')
  })

  it('is heavy for invalid UTF-8', () => {
    expect(costClass('analysis.estimate', new Uint8Array([0x7b, 0xff, 0x7d]))).toBe('heavy')
  })
})

describe('serverTimingDbMs', () => {
  it.each([
    ['db;dur=12.5, app;dur=3', 12.5],
    ['app;dur=3, db;dur=0', 0],
    ['DB;desc="query";dur=7', 7],
    ['db;dur=0.25', 0.25],
  ])('%s -> %s', (header, ms) => {
    expect(serverTimingDbMs(header)).toBe(ms)
  })

  it.each([null, '', 'app;dur=3', 'db', 'db;dur=-1', 'db;dur=abc', 'dbx;dur=5'])(
    '%s -> null',
    (header) => {
      expect(serverTimingDbMs(header)).toBeNull()
    },
  )
})

describe('settleReportFor', () => {
  const pass = (errorCode?: string): Relayed => ({
    response: new Response(null),
    outcome: 'pass',
    ...(errorCode === undefined ? {} : { errorCode }),
  })
  const json = (status: number, headers: Record<string, string> = {}): Response =>
    new Response(status === 304 ? null : '{}', {
      status,
      headers: { 'content-type': 'application/json', ...headers },
    })

  it('reports a computed 2xx with its DB time', () => {
    const upstream = json(200, { 'x-cache': 'miss', 'server-timing': 'db;dur=40, app;dur=2' })
    expect(settleReportFor(upstream, pass())).toEqual({ kind: 'computed', dbMs: 40 })
    expect(settleReportFor(json(200, { 'x-cache': 'bypass' }), pass())).toEqual({
      kind: 'computed',
      dbMs: null,
    })
  })

  it.each(['hit', 'join'])('reports X-Cache: %s as reused', (xCache) => {
    expect(settleReportFor(json(200, { 'x-cache': xCache }), pass()).kind).toBe('reused')
  })

  it.each(['overloaded', 'rate_limited', 'daily_capacity_reached'])(
    'reports the API shedding (%s)',
    (code) => {
      const status = code === 'rate_limited' ? 429 : 503
      expect(settleReportFor(json(status), pass(code)).kind).toBe('shed')
    },
  )

  it.each([
    [400, 'malformed_json'],
    [413, 'payload_too_large'],
    [422, 'validation_error'],
  ])('reports a %i as rejected', (status, code) => {
    expect(settleReportFor(json(status), pass(code)).kind).toBe('rejected')
  })

  it.each([
    [503, 'analysis_timeout'],
    [503, 'database_unavailable'],
    [503, 'database_paused'],
    [500, 'internal_error'],
  ])('reports a %i %s as computed (the DB was busy)', (status, code) => {
    expect(settleReportFor(json(status), pass(code)).kind).toBe('computed')
  })

  it('reports a platform response as not reached', () => {
    const hibernate = new Response(null, {
      status: 503,
      headers: { 'x-render-routing': 'dynamic-hibernate-error-503' },
    })
    expect(
      settleReportFor(hibernate, { response: new Response(null), outcome: 'waking' }),
    ).toEqual({ kind: 'not_reached', dbMs: null })
    const html = new Response('<p>', { status: 200, headers: { 'content-type': 'text/html' } })
    expect(
      settleReportFor(html, { response: new Response(null), outcome: 'waking' }).kind,
    ).toBe('not_reached')
  })

  it.each([520, 521, 522, 523, 525, 526, 527, 530])(
    'reports a Cloudflare %i page as not reached',
    (status) => {
      const page = new Response('<html>', { status, headers: { 'content-type': 'text/html' } })
      expect(
        settleReportFor(page, { response: new Response(null), outcome: 'unavailable' }),
      ).toEqual({ kind: 'not_reached', dbMs: null })
    },
  )

  it('reports a Cloudflare 524 as computed: the API took the analysis and kept running it', () => {
    const page = new Response('<html>', {
      status: 524,
      headers: { 'content-type': 'text/html' },
    })
    expect(
      settleReportFor(page, { response: new Response(null), outcome: 'unavailable' }),
    ).toEqual({ kind: 'computed', dbMs: null })
  })

  it('reports an API 2xx whose body broke mid-stream as computed', () => {
    const upstream = json(200, { 'x-cache': 'miss' })
    expect(
      settleReportFor(upstream, { response: new Response(null), outcome: 'unavailable' }).kind,
    ).toBe('computed')
  })
})
