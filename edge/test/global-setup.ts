/**
 * A real local HTTP origin for the integration tests (runs in Node, outside
 * workerd). It stands in for Render + the API so the Worker's network
 * behaviour is exercised over actual sockets: delayed headers, connections
 * dropped before any response, gzip-encoded bodies and the exact request
 * headers that arrive.
 *
 * The scenario is chosen by the `scenario` query parameter, which the Worker
 * forwards on GET proxy routes and analysis POSTs alike:
 *
 *   echo            200 JSON describing the request that arrived
 *   slow&delay=MS   200 JSON after MS milliseconds without any byte sent
 *   gzip            200 JSON, gzip-encoded
 *   gzip-error      429 API error envelope, gzip-encoded, Retry-After: 7
 *   error           422 API error envelope
 *   hibernate       503 empty body with Render's hibernate routing header
 *   html            200 text/html loading page
 *   reset&key=K     drops the connection before any response (counted per K)
 *   reset-once&key=K  drops the first connection for K, answers the second
 *   cacheable&dv=V&key=K  200 gzip-encoded JSON (FIXED_BODY) with the headers
 *                   the edge store gate needs (X-Cache: miss, X-Data-Version: V,
 *                   X-Analytics-Version: 0.2, X-Response-Format: 1); counted per K
 *
 * GET /_count?key=K reports how many requests a reset or cacheable scenario saw.
 * Listens on an ephemeral 127.0.0.1 port (no fixed port to collide with).
 */

import { createServer, type IncomingMessage, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { gzipSync } from 'node:zlib'
import type { TestProject } from 'vitest/node'
import { ERROR_BODY, FIXED_BODY } from './fixtures'

function readBody(req: IncomingMessage): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = []
    req.on('data', (chunk: Buffer) => chunks.push(chunk))
    req.on('end', () => resolve(Buffer.concat(chunks)))
    req.on('error', reject)
  })
}

function listen(server: Server): Promise<number> {
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve((server.address() as AddressInfo).port))
  })
}

export default async function setup(project: TestProject): Promise<() => Promise<void>> {
  const counts = new Map<string, number>()

  const server = createServer(async (req, res) => {
    const url = new URL(req.url ?? '/', 'http://127.0.0.1')
    const scenario = url.searchParams.get('scenario') ?? 'echo'
    const key = url.searchParams.get('key') ?? ''
    const body = await readBody(req)

    if (url.pathname === '/_count') {
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ count: counts.get(key) ?? 0 }))
      return
    }
    if (scenario === 'cacheable') counts.set(key, (counts.get(key) ?? 0) + 1)
    if (scenario === 'reset' || scenario === 'reset-once') {
      const seen = (counts.get(key) ?? 0) + 1
      counts.set(key, seen)
      if (scenario === 'reset' || seen === 1) {
        req.socket.destroy()
        return
      }
    }
    switch (scenario) {
      case 'slow': {
        const delay = Number(url.searchParams.get('delay') ?? '0')
        await new Promise((resolve) => setTimeout(resolve, delay))
        res.writeHead(200, {
          'Content-Type': 'application/json',
          'X-Request-ID': 'origin-slow-1',
        })
        res.end(FIXED_BODY)
        return
      }
      case 'gzip': {
        res.writeHead(200, {
          'Content-Type': 'application/json',
          'Content-Encoding': 'gzip',
          Vary: 'Accept-Encoding',
        })
        res.end(gzipSync(Buffer.from(FIXED_BODY, 'utf8')))
        return
      }
      case 'cacheable': {
        res.writeHead(200, {
          'Content-Type': 'application/json',
          'Content-Encoding': 'gzip',
          'X-Cache': 'miss',
          'X-Data-Version': url.searchParams.get('dv') ?? '',
          'X-Analytics-Version': '0.2',
          'X-Response-Format': '1',
          'X-Analysis-Key': 'e'.repeat(64),
          'Server-Timing': 'db;dur=3.2, app;dur=0.4',
        })
        res.end(gzipSync(Buffer.from(FIXED_BODY, 'utf8')))
        return
      }
      case 'gzip-error': {
        res.writeHead(429, {
          'Content-Type': 'application/json',
          'Content-Encoding': 'gzip',
          'Retry-After': '7',
        })
        res.end(gzipSync(Buffer.from(ERROR_BODY, 'utf8')))
        return
      }
      case 'error':
        res.writeHead(422, { 'Content-Type': 'application/json', server: 'uvicorn' })
        res.end(ERROR_BODY)
        return
      case 'hibernate':
        res.writeHead(503, { 'x-render-routing': 'dynamic-hibernate-error-503' })
        res.end()
        return
      case 'html':
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
        res.end('<!doctype html><title>Service waking up</title><p>Loading...</p>')
        return
      default:
        res.writeHead(200, {
          'Content-Type': 'application/json',
          server: 'uvicorn',
          'rndr-id': 'abc123',
          'x-render-origin-server': 'uvicorn',
          'Set-Cookie': 'session=origin; Path=/',
        })
        res.end(
          JSON.stringify({
            method: req.method,
            path: url.pathname,
            query: url.search,
            headers: req.headers,
            body: body.toString('utf8'),
          }),
        )
    }
  })

  const port = await listen(server)
  // A port that was just free and is now closed: connections are refused.
  const closed = createServer()
  const closedPort = await listen(closed)
  await new Promise((resolve) => closed.close(resolve))

  project.provide('localOrigin', `http://127.0.0.1:${port}`)
  project.provide('closedOrigin', `http://127.0.0.1:${closedPort}`)

  return async () => {
    server.closeAllConnections()
    await new Promise((resolve) => server.close(resolve))
  }
}
