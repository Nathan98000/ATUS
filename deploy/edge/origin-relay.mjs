// Loopback relay for the local stack's edge container (docker-compose.prod.yml,
// profile `edge`).
//
// The Worker accepts a plain-HTTP RENDER_ORIGIN only on a loopback host
// (edge/src/upstream.ts: every origin call carries the edge secret, so plain
// HTTP anywhere else is refused). In production the origin is
// https://<service>.onrender.com; in the stack the API is http://api:10000 on
// the compose network. This relay listens on 127.0.0.1 inside the edge
// container and pipes every TCP connection, unchanged, to the API service, so
// the Worker runs exactly as deployed with RENDER_ORIGIN=http://127.0.0.1:10000.
//
//   ATUS_EDGE_RELAY_LISTEN  host:port to listen on (default 127.0.0.1:10000;
//                           loopback only, so the relay never exposes the API)
//   ATUS_EDGE_RELAY_TARGET  host:port of the API (required, e.g. api:10000)
//
// Bytes are never inspected or logged; a failed connection to the API closes
// the client side, which the Worker sees as a connection failure (its
// connect-failure retry and circuit breaker then apply, as in production).
import net from 'node:net'

function hostPort(value, name) {
  const match = /^(.+):(\d{1,5})$/.exec(value ?? '')
  if (match === null || Number(match[2]) > 65535) {
    process.stderr.write(`origin-relay: ${name} must be host:port\n`)
    process.exit(2)
  }
  return { host: match[1], port: Number(match[2]) }
}

const listen = hostPort(process.env.ATUS_EDGE_RELAY_LISTEN ?? '127.0.0.1:10000', 'ATUS_EDGE_RELAY_LISTEN')
const target = hostPort(process.env.ATUS_EDGE_RELAY_TARGET, 'ATUS_EDGE_RELAY_TARGET')
if (!['127.0.0.1', 'localhost', '::1'].includes(listen.host)) {
  process.stderr.write('origin-relay: ATUS_EDGE_RELAY_LISTEN must be a loopback address\n')
  process.exit(2)
}

const server = net.createServer((client) => {
  const upstream = net.connect(target.port, target.host)
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
  process.stderr.write(`origin-relay: ${error.code ?? error.name}\n`)
  process.exit(1)
})

server.listen(listen.port, listen.host, () => {
  process.stdout.write(
    `origin-relay: ${listen.host}:${listen.port} -> ${target.host}:${target.port}\n`,
  )
})
