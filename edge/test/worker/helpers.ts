/**
 * Shared helpers for the tests that run inside workerd.
 *
 * Tests call the Worker's fetch handler directly, as Cloudflare would, with
 * the bindings from wrangler.jsonc plus the fake secrets in vitest.config.ts.
 * The origin is either a `fetch` spy (`mockOrigin`) or the real local server
 * from test/global-setup.ts (`RENDER_ORIGIN` overridden per call).
 */

import {
  createExecutionContext,
  runInDurableObject,
  waitOnExecutionContext,
} from 'cloudflare:test'
import { env } from 'cloudflare:workers'
import { expect, vi } from 'vitest'
import worker from '../../src/index'
import { GOVERNOR_NAME, type Governor } from '../../src/governor'
import { originBreaker } from '../../src/upstream'
import { resetWakeState } from '../../src/wake'

export const BASE = 'https://atus.example.workers.dev'

/** The fake edge secret configured for the tests. */
export const EDGE_SECRET = env.EDGE_AUTH_SECRET

/**
 * Clear state between tests: the per-isolate circuit breaker and last wake,
 * the governor's storage and alarm, and every KV entry.
 */
export async function resetEdgeState(): Promise<void> {
  originBreaker.recordSuccess()
  resetWakeState()
  await runInDurableObject(governorStub(), async (_instance, state) => {
    state.storage.sql.exec(
      'DELETE FROM days; DELETE FROM prefixes; DELETE FROM leases; DELETE FROM state;',
    )
    await state.storage.deleteAlarm()
  })
  const { keys } = await env.RESULT_CACHE.list()
  await Promise.all(keys.map((key) => env.RESULT_CACHE.delete(key.name)))
}

/** The governor instance the Worker talks to. */
export function governorStub(): DurableObjectStub<Governor> {
  return env.GOVERNOR.get(env.GOVERNOR.idFromName(GOVERNOR_NAME))
}

/** Run SQL inside the governor (seed or inspect its tables). */
export async function governorSql<T extends Record<string, SqlStorageValue>>(
  query: string,
  ...bindings: SqlStorageValue[]
): Promise<T[]> {
  return runInDurableObject(governorStub(), (_instance, state) =>
    state.storage.sql.exec<T>(query, ...bindings).toArray(),
  )
}

/**
 * Freeze `Date.now()` (Worker and governor alike) at a controllable instant.
 * The default is in the future, so governor alarms never fire by themselves.
 */
export function controlClock(start = Date.UTC(2036, 9, 20, 12, 0, 0)): {
  now(): number
  advance(ms: number): void
  set(ms: number): void
} {
  let now = start
  vi.spyOn(Date, 'now').mockImplementation(() => now)
  return {
    now: () => now,
    advance(ms: number) {
      now += ms
    },
    set(ms: number) {
      now = ms
    },
  }
}

/** Invoke the Worker and wait for its `waitUntil` work. */
export async function callWorker(
  input: string | Request,
  init?: RequestInit,
  overrides: Partial<Env> = {},
): Promise<Response> {
  const { response, ctx } = await callWorkerNoWait(input, init, overrides)
  await waitOnExecutionContext(ctx)
  return response
}

/** Invoke the Worker and return as soon as it responds (its `waitUntil` work may still run). */
export async function callWorkerNoWait(
  input: string | Request,
  init?: RequestInit,
  overrides: Partial<Env> = {},
): Promise<{ response: Response; ctx: ExecutionContext }> {
  const request = typeof input === 'string' ? new Request(BASE + input, init) : input
  const ctx = createExecutionContext()
  const response = await worker.fetch(
    request as Request<unknown, IncomingRequestCfProperties>,
    { ...env, ...overrides },
    ctx,
  )
  return { response, ctx }
}

export interface OriginCall {
  url: string
  method: string
  headers: Headers
  body: string | null
  init: RequestInit
}

/**
 * Replace the global `fetch` (which the Worker uses for the origin) with
 * `handler`, recording every call. Restored after each test (`restoreMocks`).
 */
export function mockOrigin(
  handler: (call: OriginCall) => Response | Promise<Response>,
): OriginCall[] {
  const calls: OriginCall[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const call: OriginCall = {
      url: input instanceof Request ? input.url : String(input),
      method: init?.method ?? 'GET',
      headers: new Headers(init?.headers),
      body: init?.body != null ? await new Response(init.body).text() : null,
      init: init ?? {},
    }
    calls.push(call)
    return handler(call)
  })
  return calls
}

/** A JSON response as the API would send it. */
export function apiJson(
  body: string,
  status = 200,
  headers: Record<string, string> = {},
): Response {
  return new Response(body, {
    status,
    headers: {
      'content-type': 'application/json',
      'x-request-id': 'origin-req-0001',
      ...headers,
    },
  })
}

/**
 * The security headers every /api response must carry, written out here
 * rather than imported from src/http.ts: a check against the source's own
 * constant could only catch a header missing from a response path, never one
 * deleted from (or weakened in) the constant (review round 2). These are the
 * only security headers browsers get on /api, since the Worker rebuilds every
 * response's headers. They match the API's own (tests/unit/api/test_guard.py)
 * plus HSTS and `Cache-Control: no-store` (docs/api.md, "Edge Worker").
 */
export const EXPECTED_SECURITY_HEADERS: Readonly<Record<string, string>> = {
  'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'",
  'Strict-Transport-Security': 'max-age=31536000',
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'Cross-Origin-Resource-Policy': 'same-origin',
  'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), payment=(), usb=()',
  'Cache-Control': 'no-store',
}

/** Assert the edge's security headers are present with their exact, pinned values. */
export function expectSecurityHeaders(response: Response): void {
  for (const [name, value] of Object.entries(EXPECTED_SECURITY_HEADERS)) {
    expect(response.headers.get(name), name).toBe(value)
  }
}

/** Parse a Worker-generated error envelope and check its shape. */
export async function readEnvelope(
  response: Response,
): Promise<{ code: string; message: string; details: Record<string, unknown> | null }> {
  expect(response.headers.get('content-type')).toBe('application/json')
  const payload = (await response.json()) as {
    error: { code: string; message: string; details: Record<string, unknown> | null }
  }
  expect(Object.keys(payload)).toEqual(['error'])
  expect(Object.keys(payload.error)).toEqual(['code', 'message', 'details'])
  return payload.error
}

/**
 * Headers a client might send to claim another address (security.md abuse
 * case 5), one per entry, each naming `address` or another network. The
 * governor charges the network of `CF-Connecting-IP` whatever they say
 * (`governor-flow.test.ts`, `metadata.test.ts`).
 */
export function forgedAddressHeaders(address: string): [name: string, value: string][] {
  return [
    ['X-Forwarded-For', address],
    ['X-Forwarded-For', `${address}, 192.0.2.250`],
    ['X-Real-IP', address],
    ['True-Client-IP', address],
    ['X-Client-IP', address],
    ['X-ATUS-Client-IP', address],
    ['Forwarded', `for=${address};proto=https`],
    ['CF-Connecting-IPv6', '2001:db8:ffff::1'],
  ]
}
