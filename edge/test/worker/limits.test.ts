/** Analysis POST limits (ADR-011 step 3.2): JSON only (415) and 64 KiB (413). */

import { beforeEach, describe, expect, it } from 'vitest'
import { MAX_BODY_BYTES } from '../../src/constants'
import {
  BASE,
  apiJson,
  callWorker,
  expectSecurityHeaders,
  mockOrigin,
  readEnvelope,
  resetEdgeState,
} from './helpers'

const PATH = '/api/v1/analysis/estimate'

beforeEach(resetEdgeState)

/** A JSON document of exactly `size` bytes. */
function jsonOfSize(size: number): string {
  const frame = '{"pad":""}'
  return `{"pad":"${'x'.repeat(size - frame.length)}"}`
}

/** A body stream with no declared length, delivered in 1 KiB chunks. */
function streamOf(text: string): ReadableStream<Uint8Array> {
  const bytes = new TextEncoder().encode(text)
  let offset = 0
  return new ReadableStream({
    pull(controller) {
      if (offset >= bytes.length) {
        controller.close()
        return
      }
      controller.enqueue(bytes.slice(offset, offset + 1024))
      offset += 1024
    },
  })
}

describe('body cap', () => {
  it('forwards a body of exactly 64 KiB, byte for byte', async () => {
    const body = jsonOfSize(MAX_BODY_BYTES)
    expect(new TextEncoder().encode(body).length).toBe(65536)
    const calls = mockOrigin(() => apiJson('{"ok":true}'))
    const response = await callWorker(PATH, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body,
    })
    expect(response.status).toBe(200)
    expect(calls).toHaveLength(1)
    expect(calls[0]!.body).toBe(body)
  })

  it('rejects one byte more with 413 payload_too_large and no origin call', async () => {
    const calls = mockOrigin(() => apiJson('{"ok":true}'))
    const response = await callWorker(PATH, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: jsonOfSize(MAX_BODY_BYTES + 1),
    })
    expect(response.status).toBe(413)
    expect(calls).toHaveLength(0)
    expectSecurityHeaders(response)
    const error = await readEnvelope(response)
    expect(error.code).toBe('payload_too_large')
    expect(error.details).toEqual({ max_bytes: 65536 })
  })

  it('cuts off a streamed body without a declared length', async () => {
    const calls = mockOrigin(() => apiJson('{"ok":true}'))
    const request = new Request(BASE + PATH, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: streamOf(jsonOfSize(MAX_BODY_BYTES * 4)),
    })
    expect(request.headers.get('content-length')).toBeNull()
    const response = await callWorker(request)
    expect(response.status).toBe(413)
    expect(calls).toHaveLength(0)
  })

  it('forwards a small streamed body without a declared length', async () => {
    const calls = mockOrigin(() => apiJson('{"ok":true}'))
    const body = '{"analysis":"estimate","years":[2024]}'
    const response = await callWorker(
      new Request(BASE + PATH, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: streamOf(body),
      }),
    )
    expect(response.status).toBe(200)
    expect(calls[0]!.body).toBe(body)
  })

  it('refuses a declared Content-Length over the cap before reading the body', async () => {
    const calls = mockOrigin(() => apiJson('{"ok":true}'))
    let pulled = false
    // highWaterMark 0: `pull` runs only when someone reads.
    const body = new ReadableStream<Uint8Array>(
      {
        pull(controller) {
          pulled = true
          controller.close()
        },
      },
      { highWaterMark: 0 },
    )
    const response = await callWorker(
      new Request(BASE + PATH, {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'content-length': '1000000' },
        body,
      }),
    )
    expect(response.status).toBe(413)
    expect(pulled).toBe(false)
    expect(calls).toHaveLength(0)
  })
})

describe('JSON only', () => {
  it.each([
    ['no Content-Type', undefined],
    ['text/plain', 'text/plain'],
    ['form data', 'application/x-www-form-urlencoded'],
    ['multipart', 'multipart/form-data; boundary=x'],
    ['a JSON look-alike', 'application/jsonp'],
    ['problem+json', 'application/problem+json'],
  ])('%s gives 415 unsupported_media_type and no origin call', async (_, contentType) => {
    const calls = mockOrigin(() => apiJson('{"ok":true}'))
    const headers: Record<string, string> = contentType ? { 'content-type': contentType } : {}
    const response = await callWorker(PATH, {
      method: 'POST',
      headers,
      body: new TextEncoder().encode('{}'),
    })
    expect(response.status).toBe(415)
    expect(calls).toHaveLength(0)
    expectSecurityHeaders(response)
    expect((await readEnvelope(response)).code).toBe('unsupported_media_type')
  })

  it.each(['application/json', 'application/json; charset=utf-8', 'Application/JSON'])(
    'accepts %s and forwards it unchanged',
    async (contentType) => {
      const calls = mockOrigin(() => apiJson('{"ok":true}'))
      const response = await callWorker(PATH, {
        method: 'POST',
        headers: { 'content-type': contentType },
        body: '{}',
      })
      expect(response.status).toBe(200)
      expect(calls[0]!.headers.get('content-type')).toBe(contentType)
    },
  )

  it('forwards an empty JSON POST for the API to judge', async () => {
    const calls = mockOrigin(() =>
      apiJson('{"error":{"code":"malformed_json","message":"x","details":null}}', 400),
    )
    const response = await callWorker(PATH, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
    })
    expect(calls).toHaveLength(1)
    expect(calls[0]!.body ?? '').toBe('')
    expect(response.status).toBe(400)
  })
})
