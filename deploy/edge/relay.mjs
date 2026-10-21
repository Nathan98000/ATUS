// Loopback TCP relay for the local `edge` profile of docker-compose.prod.yml.
//
//   node relay.mjs 127.0.0.1:10000 api:10000
//
// The Worker accepts a plain-HTTP origin only on a loopback host (every
// upstream call carries the edge secret; edge/src/upstream.ts originUrl), so
// in the local stack, where the API is another container, the entrypoint
// starts this relay and points RENDER_ORIGIN at its loopback address. Bytes
// are passed through unchanged; nothing is logged but start-up and errors
// (never request contents). Local stack only: production uses https.
import { createConnection, createServer } from 'node:net'

function hostPort(value, what) {
  const match = /^(\[[^\]]+\]|[^:]+):(\d{1,5})$/.exec(value ?? '')
  if (!match || Number(match[2]) > 65535) {
    console.error(`relay: ${what} must be <host>:<port>`)
    process.exit(2)
  }
  return { host: match[1].replace(/^\[|\]$/g, ''), port: Number(match[2]) }
}

const listen = hostPort(process.argv[2], 'the listen address')
const target = hostPort(process.argv[3], 'the target')
if (!['127.0.0.1', 'localhost', '::1'].includes(listen.host)) {
  console.error('relay: listens on loopback only')
  process.exit(2)
}

const server = createServer((client) => {
  const upstream = createConnection(target)
  const close = () => {
    client.destroy()
    upstream.destroy()
  }
  client.on('error', close)
  upstream.on('error', close)
  client.pipe(upstream)
  upstream.pipe(client)
})
server.on('error', (error) => {
  console.error(`relay: ${error.code ?? error.name}`)
  process.exit(1)
})
server.listen(listen.port, listen.host, () => {
  console.log(`relay: ${listen.host}:${listen.port} -> ${target.host}:${target.port}`)
})
