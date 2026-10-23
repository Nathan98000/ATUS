/**
 * Metadata GETs answered from the static snapshot (ADR-015 tier L0).
 *
 * The deploy publishes every metadata response the API gives — `/meta`,
 * `/activities`, `/activities/presets`, `/population/metadata` and every
 * activity detail — byte for byte under `/snap/<SNAPSHOT_ID>/get/`, built
 * from the deployed image on the release data. The SPA reads those files
 * directly; the Worker answers the same routes from them too, so a metadata
 * GET costs Render nothing: forwarded, each would cost Render's monthly
 * outbound bandwidth (up to ~9 KB gzipped for the lexicon), and one client
 * replaying them could spend it within days.
 *
 * Only a request without a query string is answered here, and only when the
 * deployment names its snapshot (`SNAPSHOT_ID`) and release (`DATA_VERSION`,
 * `ANALYTICS_VERSION`), and the snapshot id carries the hash of that
 * `DATA_VERSION` (`<api_build[:12]>-<sha256(DATA_VERSION)[:16]>`, as
 * scripts/build_snapshot.py names it), so the `X-Data-Version` the Worker
 * adds is the snapshot's own. The response has the API's version headers
 * and the Worker's security headers. A file the snapshot does not hold gets
 * the SPA's `index.html` from the assets binding, never JSON: that is a miss,
 * and the caller sends the request to the origin as a governed lookup.
 */

import { applySecurityHeaders, isJsonContentType, sha256Hex } from './http'

/** The fixed metadata routes and their snapshot files. */
const FILES: ReadonlyMap<string, string> = new Map([
  ['/api/v1/meta', 'get/meta.json'],
  ['/api/v1/activities', 'get/activities.json'],
  ['/api/v1/activities/presets', 'get/activities/presets.json'],
  ['/api/v1/population/metadata', 'get/population/metadata.json'],
])

/** A lexicon code (2, 4 or 6 digits): only these have a detail file. */
const ACTIVITY_DETAIL = /^\/api\/v1\/activities\/([0-9]{2}(?:[0-9]{2}){0,2})$/

/** The data-independent vocabularies: the API sends them without `X-Data-Version`. */
const UNVERSIONED = new Set(['/api/v1/activities/presets', '/api/v1/population/metadata'])

/** As scripts/build_snapshot.py validates it. */
const SNAPSHOT_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/

type SnapshotVars = Pick<Env, 'ASSETS' | 'SNAPSHOT_ID' | 'DATA_VERSION' | 'ANALYTICS_VERSION'>

/** The snapshot file that holds `pathname`'s response, or null. */
export function snapshotFile(pathname: string): string | null {
  const fixed = FILES.get(pathname)
  if (fixed !== undefined) return fixed
  const detail = ACTIVITY_DETAIL.exec(pathname)
  return detail === null ? null : `get/activities/${detail[1]}.json`
}

/**
 * The snapshot's answer to a metadata GET, or null when the snapshot cannot
 * give one (no query string allowed; see the module comment).
 */
export async function readSnapshot(
  env: SnapshotVars,
  url: URL,
  requestId: string,
): Promise<Response | null> {
  const { SNAPSHOT_ID: id, DATA_VERSION: dataVersion, ANALYTICS_VERSION: analytics } = env
  if (url.search !== '' || !id || !dataVersion || !analytics || !SNAPSHOT_ID.test(id)) {
    return null
  }
  const file = snapshotFile(url.pathname)
  if (file === null) return null
  if (!id.endsWith(`-${(await sha256Hex(dataVersion)).slice(0, 16)}`)) return null
  let asset: Response
  try {
    asset = await env.ASSETS.fetch(new URL(`/snap/${id}/${file}`, url.origin))
  } catch {
    return null
  }
  if (asset.status !== 200 || !isJsonContentType(asset.headers.get('content-type'))) {
    try {
      await asset.body?.cancel()
    } catch {
      // Nothing to release.
    }
    return null
  }
  const headers = new Headers({ 'Content-Type': 'application/json', 'X-Request-ID': requestId })
  if (!UNVERSIONED.has(url.pathname)) headers.set('X-Data-Version', dataVersion)
  if (url.pathname === '/api/v1/meta') headers.set('X-Analytics-Version', analytics)
  applySecurityHeaders(headers)
  return new Response(asset.body, { status: 200, headers })
}
