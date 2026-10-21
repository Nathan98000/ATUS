/**
 * GET /api/v1/wake: user-intent prewake (ADR-011 "Prewake").
 *
 * The SPA calls it when a user enters Explore or opens a share link that is
 * not in the static snapshot, so Render's ~1 minute spin-up overlaps the time
 * spent building a spec. The Worker races origin `/health` (which never
 * touches the database, so Neon is never woken) against `WAKE_RACE_MS` and
 * answers
 *
 *     {"origin": "warm" | "waking" | "unknown", "wake": "sent" | "recent" | "off" | "circuit_open"}
 *
 * - `warm`: origin /health answered 2xx JSON within the race (now or, for
 *   `recent`, on the last wake); `waking`: it has not (yet); `unknown`: no
 *   wake was attempted or the origin could not be reached.
 * - The /health call outlives the reply through `waitUntil`, but only for
 *   the 30 s Cloudflare allows after a response: a spin-up that answers
 *   within that time is recorded; a longer one (Render documents about a
 *   minute) is cut off with the probe and stays recorded as `waking`.
 * - Later calls report the last wake's result (`recent`) without an origin
 *   call: for `WAKE_INTERVAL_MS` once the origin answered warm, but only for
 *   `WAKE_RETRY_MS` while it was `waking` or `unknown`, so a stale "waking"
 *   does not keep the SPA's cold-start notice up on a warm service. At most
 *   one wake per isolate per minute while the origin is waking, per 10
 *   minutes once it is warm.
 * - The Worker var `PREWAKE=off` disables the origin call entirely.
 */

import { WAKE_INTERVAL_MS, WAKE_RACE_MS, WAKE_RETRY_MS } from './constants'
import { isJsonContentType, jsonResponse } from './http'
import { buildUpstreamHeaders } from './proxy'
import { fetchOrigin, originBreaker } from './upstream'

export type OriginState = 'warm' | 'waking' | 'unknown'

interface WakeRecord {
  sentAt: number
  origin: OriginState
}

/** Per-isolate: the last wake this isolate sent. */
let lastWake: WakeRecord | null = null

/** Forget the last wake (tests). */
export function resetWakeState(): void {
  lastWake = null
}

/**
 * Answer a prewake request. `target` is the origin /health URL and
 * `edgeSecret` the configured edge secret (the caller has checked both).
 */
export async function handleWake(
  request: Request,
  env: Pick<Env, 'PREWAKE'>,
  ctx: ExecutionContext,
  requestId: string,
  target: string,
  edgeSecret: string,
): Promise<Response> {
  if (env.PREWAKE === 'off') {
    return jsonResponse(200, { origin: 'unknown', wake: 'off' }, requestId)
  }
  const now = Date.now()
  if (lastWake !== null) {
    const remembered = lastWake.origin === 'warm' ? WAKE_INTERVAL_MS : WAKE_RETRY_MS
    if (now - lastWake.sentAt < remembered) {
      return jsonResponse(200, { origin: lastWake.origin, wake: 'recent' }, requestId)
    }
  }
  if (originBreaker.remainingOpenMs(now) > 0) {
    return jsonResponse(200, { origin: 'unknown', wake: 'circuit_open' }, requestId)
  }

  const record: WakeRecord = { sentAt: now, origin: 'waking' }
  lastWake = record
  const probe = probeHealth(request, requestId, target, edgeSecret).then((state) => {
    record.origin = state
    return state
  })
  ctx.waitUntil(probe)

  let timer: ReturnType<typeof setTimeout> | null = null
  const timeout = new Promise<OriginState>((resolve) => {
    timer = setTimeout(() => resolve('waking'), WAKE_RACE_MS)
  })
  const origin = await Promise.race([probe, timeout])
  clearTimeout(timer)
  return jsonResponse(200, { origin, wake: 'sent' }, requestId)
}

async function probeHealth(
  request: Request,
  requestId: string,
  target: string,
  edgeSecret: string,
): Promise<OriginState> {
  const outcome = await fetchOrigin(target, {
    method: 'GET',
    headers: buildUpstreamHeaders(request, requestId, edgeSecret, false),
    redirect: 'manual',
  })
  if (outcome.kind !== 'response') return 'unknown'
  const { response } = outcome
  const warm = response.ok && isJsonContentType(response.headers.get('content-type'))
  try {
    await response.body?.cancel()
  } catch {
    // Nothing to release.
  }
  return warm ? 'warm' : 'waking'
}
