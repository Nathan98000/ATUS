/**
 * Edge result cache, tier L1 (ADR-011 "Edge tiers"; ADR-015).
 *
 * Workers KV holds origin analysis responses, byte for byte, under
 *
 *     r1/<CACHE_SCOPE>/<k>
 *     k           = sha256_hex(path + "?" + query + "\n" + body)
 *     CACHE_SCOPE = sha256_hex(EPOCH|API_VERSION|ANALYTICS_VERSION|DATA_VERSION|RESPONSE_FORMAT)[:16]
 *
 * `k` is computed over the exact request bytes (no JSON canonicalization:
 * a round trip is lossy, so two different requests could share a key). The
 * SPA sends a stable sorted-keys serialization, so the same spec always has
 * the same bytes. The scope changes with every release var, so a new data
 * or analytics version never reads an old entry; the git SHA is not in it,
 * so a code-only deploy keeps the cache.
 *
 * Reads happen before the governor for analysis POSTs; a failed read is a
 * miss. A response is stored (through `waitUntil`, after it was sent) only
 * when the store gate holds — see `storeHeadersAllow` — and the complete
 * body is at most 16 KiB. A failed write is ignored. Errors are never
 * stored. With an empty `DATA_VERSION` or `ANALYTICS_VERSION` (a deploy
 * without release vars) the cache is off: no reads, no writes.
 */

import { KV_KEY_VERSION, KV_MAX_VALUE_BYTES, KV_TTL_SECONDS } from './constants'
import { applySecurityHeaders, concatBytes, isJsonContentType, sha256Hex } from './http'

/** Origin `X-Cache` values a stored response may carry: computed or served from the API's own cache. */
const STORABLE_ORIGIN_CACHE = new Set(['miss', 'hit'])

/** KV's documented metadata limit (serialized JSON). */
const MAX_METADATA_BYTES = 1024

/** Stored with every entry and replayed as response headers on a hit. */
export interface CachedMeta {
  content_type: string
  data_version: string
  analytics_version: string
  response_format: string
  analysis_key: string | null
  stored_at: string
}

type ReleaseVars = Pick<
  Env,
  'EPOCH' | 'API_VERSION' | 'ANALYTICS_VERSION' | 'DATA_VERSION' | 'RESPONSE_FORMAT'
>

/** This deployment's cache scope, or `null` when the release vars are unset (cache off). */
export async function cacheScope(env: ReleaseVars): Promise<string | null> {
  const parts = [
    env.EPOCH,
    env.API_VERSION,
    env.ANALYTICS_VERSION,
    env.DATA_VERSION,
    env.RESPONSE_FORMAT,
  ]
  if (parts.some((part) => !part)) return null
  return (await sha256Hex(parts.join('|'))).slice(0, 16)
}

/** The request hash shared with the SPA and the snapshot manifest. */
export async function requestHash(
  path: string,
  query: string,
  body: Uint8Array,
): Promise<string> {
  const head = new TextEncoder().encode(`${path}?${query}\n`)
  return sha256Hex(concatBytes([head, body]))
}

export function kvKey(scope: string, hash: string): string {
  return `${KV_KEY_VERSION}/${scope}/${hash}`
}

/**
 * The stored response for `key` as a 200 with `X-Cache: edge`, `'miss'`, or
 * `'error'` when KV failed (also a miss for the caller). An entry whose
 * metadata does not match this deployment's release vars is a miss.
 */
export async function readCached(
  kv: KVNamespace,
  key: string,
  env: ReleaseVars,
  requestId: string,
): Promise<Response | 'miss' | 'error'> {
  let entry: KVNamespaceGetWithMetadataResult<ArrayBuffer, CachedMeta>
  try {
    entry = await kv.getWithMetadata<CachedMeta>(key, { type: 'arrayBuffer' })
  } catch {
    return 'error'
  }
  const { value, metadata } = entry
  if (value === null || value.byteLength === 0 || value.byteLength > KV_MAX_VALUE_BYTES) {
    return 'miss'
  }
  if (
    metadata === null ||
    !isJsonContentType(metadata.content_type) ||
    metadata.data_version !== env.DATA_VERSION ||
    metadata.analytics_version !== env.ANALYTICS_VERSION ||
    metadata.response_format !== env.RESPONSE_FORMAT
  ) {
    return 'miss'
  }
  const headers = new Headers({
    'Content-Type': metadata.content_type,
    'X-Data-Version': metadata.data_version,
    'X-Analytics-Version': metadata.analytics_version,
    'X-Response-Format': metadata.response_format,
    'X-Cache': 'edge',
    'X-Request-ID': requestId,
  })
  if (metadata.analysis_key) headers.set('X-Analysis-Key', metadata.analysis_key)
  applySecurityHeaders(headers)
  return new Response(value, { status: 200, headers })
}

/**
 * The header half of the store gate (ADR-011): a 200 JSON response the API
 * computed or served from its own cache (`X-Cache: miss|hit`, never
 * `bypass` or `join`), for exactly this deployment's data version, analytics
 * version and response format. The caller adds the rest: a POST to an
 * analysis path and a complete body of at most 16 KiB.
 */
export function storeHeadersAllow(upstream: Response, env: ReleaseVars): boolean {
  const { headers } = upstream
  return (
    upstream.status === 200 &&
    isJsonContentType(headers.get('content-type')) &&
    STORABLE_ORIGIN_CACHE.has(headers.get('x-cache') ?? '') &&
    Boolean(env.DATA_VERSION) &&
    headers.get('x-data-version') === env.DATA_VERSION &&
    headers.get('x-analytics-version') === env.ANALYTICS_VERSION &&
    headers.get('x-response-format') === env.RESPONSE_FORMAT
  )
}

/** The metadata to store with a response that passed `storeHeadersAllow`. */
export function cachedMeta(upstream: Response, now: number): CachedMeta {
  const { headers } = upstream
  return {
    content_type: headers.get('content-type') ?? 'application/json',
    data_version: headers.get('x-data-version') ?? '',
    analytics_version: headers.get('x-analytics-version') ?? '',
    response_format: headers.get('x-response-format') ?? '',
    analysis_key: headers.get('x-analysis-key'),
    stored_at: new Date(now).toISOString(),
  }
}

/**
 * Store `body` under `key` for 30 days. Returns whether KV accepted it;
 * never rejects. Empty or oversized bodies and oversized metadata are not
 * stored.
 */
export async function storeCached(
  kv: KVNamespace,
  key: string,
  body: Uint8Array,
  meta: CachedMeta,
): Promise<boolean> {
  if (body.byteLength === 0 || body.byteLength > KV_MAX_VALUE_BYTES) return false
  if (new TextEncoder().encode(JSON.stringify(meta)).byteLength > MAX_METADATA_BYTES)
    return false
  try {
    await kv.put(key, body, { expirationTtl: KV_TTL_SECONDS, metadata: meta })
    return true
  } catch {
    return false
  }
}
