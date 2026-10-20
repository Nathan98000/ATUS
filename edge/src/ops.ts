/**
 * GET /api/_ops/budget (ADR-011 "Visibility"): the governor's budget state
 * for the owner and the daily quota-report workflow.
 *
 * Only `Authorization: Bearer <OPS_TOKEN>` gets it; anything else gets the
 * same 404 envelope as an unknown route, so the endpoint is not revealed.
 * The token is compared in constant time (SHA-256 digests of both sides,
 * then `timingSafeEqual`). The report has counts and budgets only — no IPs
 * and no prefix hashes.
 */

import { readBudget } from './admission'
import { RETRY_AFTER_GOVERNOR_S } from './constants'
import { cacheScope } from './cache'
import { errorResponse, jsonResponse, notFoundResponse } from './http'

/** True when `authorization` is exactly `Bearer <token>` and the token is configured. */
export async function bearerMatches(
  authorization: string | null,
  token: string | undefined,
): Promise<boolean> {
  if (!token || authorization === null) return false
  const match = /^Bearer ([^\s]+)$/i.exec(authorization)
  if (match === null) return false
  const encoder = new TextEncoder()
  const [given, expected] = await Promise.all([
    crypto.subtle.digest('SHA-256', encoder.encode(match[1])),
    crypto.subtle.digest('SHA-256', encoder.encode(token)),
  ])
  return crypto.subtle.timingSafeEqual(given, expected)
}

/** 503 `capacity_unavailable`: the governor could not be reached. */
export function capacityUnavailable(requestId: string): Response {
  return errorResponse(
    503,
    'capacity_unavailable',
    'New analyses are temporarily unavailable because the capacity budget could not be checked. ' +
      'Saved and prewarmed results still work.',
    requestId,
    { retryAfterSeconds: RETRY_AFTER_GOVERNOR_S },
  )
}

export async function handleBudget(
  request: Request,
  env: Env,
  requestId: string,
): Promise<Response> {
  if (!(await bearerMatches(request.headers.get('authorization'), env.OPS_TOKEN))) {
    return notFoundResponse(requestId)
  }
  let governor
  try {
    governor = await readBudget(env)
  } catch {
    return capacityUnavailable(requestId)
  }
  return jsonResponse(
    200,
    {
      status: 'ok',
      edge: {
        api_version: env.API_VERSION,
        analytics_version: env.ANALYTICS_VERSION || null,
        data_version: env.DATA_VERSION || null,
        response_format: env.RESPONSE_FORMAT,
        snapshot_id: env.SNAPSHOT_ID || null,
        cache_scope: await cacheScope(env),
      },
      governor,
    },
    requestId,
  )
}
