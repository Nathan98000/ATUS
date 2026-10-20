/**
 * Shared helpers for the tests that run inside workerd.
 *
 * Tests call the Worker's fetch handler directly, as Cloudflare would, with
 * the bindings from wrangler.jsonc plus the fake secrets in vitest.config.ts.
 * The origin is either a `fetch` spy (`mockOrigin`) or the real local server
 * from test/global-setup.ts (`RENDER_ORIGIN` overridden per call).
 */

import { createExecutionContext, waitOnExecutionContext } from 'cloudflare:test'
import { env } from 'cloudflare:workers'
import { expect, vi } from 'vitest'
import worker from '../../src/index'
import { SECURITY_HEADERS } from '../../src/http'
import { originBreaker } from '../../src/upstream'
import { resetWakeState } from '../../src/wake'

export const BASE = 'https://atus.example.workers.dev'

/** The fake edge secret configured for the tests. */
export const EDGE_SECRET = env.EDGE_AUTH_SECRET

/** Clear per-isolate state (circuit breaker, last wake) between tests. */
export function resetEdgeState(): void {
  originBreaker.recordSuccess()
  resetWakeState()
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

/** Assert the edge's security headers are present with their exact values. */
export function expectSecurityHeaders(response: Response): void {
  for (const [name, value] of Object.entries(SECURITY_HEADERS)) {
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
