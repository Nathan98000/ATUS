/**
 * The Worker's side of the quota governor (governor.ts; ADR-015): who an
 * uncached analysis is charged to, what it costs, the RPC calls and their
 * failure policy, and how its outcome is reported back.
 *
 * - Client: an HMAC of the IPv4 /24 or IPv6 /48 of `CF-Connecting-IP`,
 *   under a key derived from `IP_HMAC_KEY` and the UTC date, so it rotates
 *   daily and no IP or reversible hash ever reaches the governor's storage.
 * - Cost: heavy when `len(years)` (doubled for a compare) is 8 or more —
 *   the API's lane classifier — or when the body cannot be read as JSON
 *   with a `years` array (the API rejects those anyway, and they are
 *   refunded).
 * - Failure: if the governor cannot be reached or does not answer within
 *   `GOVERNOR_TIMEOUT_MS`, the analysis is refused (fail closed); settling
 *   never fails a request.
 */

import { utcDay } from './budget'
import { GOVERNOR_TIMEOUT_MS, HEAVY_YEAR_QUERIES } from './constants'
import {
  GOVERNOR_NAME,
  type AdmitRequest,
  type Governor,
  type BudgetReport,
  type CostClass,
  type Denied,
  type SettleKind,
  type SettleReport,
} from './governor'
import { isJsonContentType, toHex } from './http'
import type { Relayed } from './proxy'

/** API error codes that mean "refused without computing". */
const SHED_CODES = new Set(['overloaded', 'rate_limited', 'daily_capacity_reached'])

/** Origin `X-Cache` values for which the API did not compute this request itself. */
const REUSED_ORIGIN_CACHE = new Set(['hit', 'join'])

/**
 * Cloudflare's 524 from Render's zone: the origin took the request but sent
 * no response within the proxy read timeout. Unlike the other 52x pages, the
 * API did receive the analysis, and it runs an admitted analysis to the end
 * even when the client is gone (ADR-011), so the DB was busy: it is charged.
 */
const ORIGIN_TIMEOUT_STATUS = 524

export type Admission =
  | { kind: 'admitted'; leaseId: string }
  | { kind: 'denied'; denial: Denied }
  | { kind: 'unavailable' }

// --- client prefix ------------------------------------------------------------

function parseIpv4(text: string): number[] | null {
  const match = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(text)
  if (match === null) return null
  const octets = match.slice(1).map(Number)
  return octets.every((octet) => octet <= 255) ? octets : null
}

function parseIpv6(text: string): number[] | null {
  let source = text.toLowerCase()
  // A trailing dotted quad (::ffff:192.0.2.1) becomes two hex groups.
  const dotted = /^(.*:)(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})$/.exec(source)
  if (dotted !== null) {
    const v4 = parseIpv4(dotted[2]!)
    if (v4 === null) return null
    source =
      dotted[1]! +
      ((v4[0]! << 8) | v4[1]!).toString(16) +
      ':' +
      ((v4[2]! << 8) | v4[3]!).toString(16)
  }
  const halves = source.split('::')
  if (halves.length > 2) return null
  const head = halves[0] ? halves[0].split(':') : []
  const tail = halves.length === 2 && halves[1] ? halves[1].split(':') : []
  const missing = 8 - head.length - tail.length
  if (halves.length === 1 ? missing !== 0 : missing < 1) return null
  const groups = [
    ...head,
    ...Array<string>(halves.length === 2 ? missing : 0).fill('0'),
    ...tail,
  ]
  if (!groups.every((group) => /^[0-9a-f]{1,4}$/.test(group))) return null
  return groups.map((group) => parseInt(group, 16))
}

/**
 * The network a client address is charged to: `a.b.c.0/24` for IPv4 (also
 * when IPv4-mapped in IPv6), `xxxx:xxxx:xxxx::/48` for IPv6, and `unknown`
 * when there is no parseable address (one shared bucket).
 */
export function ipPrefix(ip: string | null): string {
  if (ip === null) return 'unknown'
  const v4 = parseIpv4(ip)
  if (v4 !== null) return `${v4[0]}.${v4[1]}.${v4[2]}.0/24`
  const v6 = parseIpv6(ip)
  if (v6 === null) return 'unknown'
  const mapped = v6.slice(0, 5).every((group) => group === 0) && v6[5] === 0xffff
  if (mapped) return `${v6[6]! >> 8}.${v6[6]! & 0xff}.${v6[7]! >> 8}.0/24`
  return `${v6
    .slice(0, 3)
    .map((group) => group.toString(16).padStart(4, '0'))
    .join(':')}::/48`
}

async function hmac(key: BufferSource, message: string): Promise<ArrayBuffer> {
  const cryptoKey = await crypto.subtle.importKey(
    'raw',
    key,
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  )
  return crypto.subtle.sign('HMAC', cryptoKey, new TextEncoder().encode(message))
}

/**
 * HMAC-SHA256 of `prefix` under the day key HMAC-SHA256(secret,
 * "atus-ip-prefix|<UTC date>"), truncated to 128 bits (hex). Without the
 * secret, a stored value cannot be linked to a network or to another day.
 */
export async function prefixHmac(secret: string, now: number, prefix: string): Promise<string> {
  const dayKey = await hmac(new TextEncoder().encode(secret), `atus-ip-prefix|${utcDay(now)}`)
  return toHex(await hmac(dayKey, prefix)).slice(0, 32)
}

// --- cost class ----------------------------------------------------------------

/** Light or heavy, from the number of year queries the request asks for. */
export function costClass(routeName: string, body: Uint8Array): CostClass {
  let years: unknown
  try {
    const payload: unknown = JSON.parse(
      new TextDecoder('utf-8', { fatal: true, ignoreBOM: false }).decode(body),
    )
    years = (payload as { years?: unknown } | null)?.years
  } catch {
    return 'heavy'
  }
  if (!Array.isArray(years)) return 'heavy'
  const yearQueries = years.length * (routeName === 'analysis.compare' ? 2 : 1)
  return yearQueries >= HEAVY_YEAR_QUERIES ? 'heavy' : 'light'
}

// --- RPC ------------------------------------------------------------------------

function governorStub(env: Pick<Env, 'GOVERNOR'>): DurableObjectStub<Governor> {
  return env.GOVERNOR.get(env.GOVERNOR.idFromName(GOVERNOR_NAME))
}

/** `promise`, or a rejection after `GOVERNOR_TIMEOUT_MS`. */
async function withTimeout<T>(promise: Promise<T>): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | null = null
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error('governor timeout')), GOVERNOR_TIMEOUT_MS)
  })
  try {
    return await Promise.race([promise, timeout])
  } finally {
    if (timer !== null) clearTimeout(timer)
  }
}

/** Ask the governor to admit an uncached analysis. Never rejects. */
export async function admitAnalysis(
  env: Pick<Env, 'GOVERNOR'>,
  request: AdmitRequest,
): Promise<Admission> {
  let result: unknown
  try {
    result = await withTimeout<unknown>(governorStub(env).admit(request))
  } catch {
    return { kind: 'unavailable' }
  }
  const answer = result as Partial<{ ok: boolean; leaseId: unknown; code: unknown }> | null
  if (answer?.ok === true && typeof answer.leaseId === 'string') {
    return { kind: 'admitted', leaseId: answer.leaseId }
  }
  if (answer?.ok === false && typeof answer.code === 'string') {
    return { kind: 'denied', denial: result as Denied }
  }
  return { kind: 'unavailable' }
}

/** Report how an admitted analysis ended. Never rejects; a lost settle expires in full. */
export async function settleAnalysis(
  env: Pick<Env, 'GOVERNOR'>,
  leaseId: string,
  report: SettleReport,
): Promise<boolean> {
  try {
    await withTimeout<unknown>(governorStub(env).settle(leaseId, report))
    return true
  } catch {
    return false
  }
}

/** The governor's budget report for the ops endpoint (rejects when unreachable). */
export async function readBudget(env: Pick<Env, 'GOVERNOR'>): Promise<BudgetReport> {
  return (await withTimeout<unknown>(governorStub(env).budget())) as BudgetReport
}

// --- settling -------------------------------------------------------------------

/** `db;dur=<ms>` from a `Server-Timing` header, or null when absent or malformed. */
export function serverTimingDbMs(header: string | null): number | null {
  if (header === null) return null
  for (const metric of header.split(',')) {
    const [name, ...params] = metric.split(';').map((part) => part.trim())
    if (name?.toLowerCase() !== 'db') continue
    for (const param of params) {
      const match = /^dur=([0-9]+(?:\.[0-9]+)?)$/i.exec(param)
      if (match !== null) return Number(match[1])
    }
    return null
  }
  return null
}

export const NOT_REACHED: SettleReport = { kind: 'not_reached', dbMs: null }

/**
 * Classify an origin response for `settle` (see `SettleKind`). A relayed
 * platform page means the API never took the request, except a 524 (the
 * API was still computing); a 2xx JSON whose body broke mid-stream did come
 * from the API, which computed it.
 *
 * `attempts` is how many origin calls it took. After a retry, a `reused`
 * answer (X-Cache hit or join, `db;dur=0`) may be the first attempt's own
 * computation, which reached the API before its connection dropped: it is
 * charged as computed, from admission, rather than refunded.
 */
export function settleReportFor(
  upstream: Response,
  relayed: Relayed,
  attempts = 1,
): SettleReport {
  const { status, headers } = upstream
  const dbMs = serverTimingDbMs(headers.get('server-timing'))
  const apiSuccess =
    status >= 200 && status < 300 && isJsonContentType(headers.get('content-type'))
  if (relayed.outcome !== 'pass' && !apiSuccess) {
    return status === ORIGIN_TIMEOUT_STATUS ? { kind: 'computed', dbMs: null } : NOT_REACHED
  }
  let kind: SettleKind
  if (status === 304 || (apiSuccess && REUSED_ORIGIN_CACHE.has(headers.get('x-cache') ?? ''))) {
    kind = 'reused'
  } else if (apiSuccess) {
    kind = 'computed'
  } else if (relayed.errorCode !== undefined && SHED_CODES.has(relayed.errorCode)) {
    kind = 'shed'
  } else if (status >= 400 && status < 500) {
    kind = 'rejected'
  } else {
    kind = 'computed'
  }
  if (kind === 'reused' && attempts > 1) return { kind: 'computed', dbMs: null }
  return { kind, dbMs }
}
