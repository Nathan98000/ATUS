/**
 * The static result snapshot (ADR-011, tier L0).
 *
 * Each deploy publishes, next to the SPA, the exact API responses for all
 * metadata and for about 130 prewarmed analyses. Static files never invoke
 * the edge Worker, so they keep working when the API sleeps, when a quota is
 * exhausted, and when the Worker's daily request allowance runs out:
 *
 * ```text
 * /snap/current.json                  no-cache pointer: snapshot_id, data_version, …
 * /snap/<snapshot_id>/manifest.json   immutable: { get: {path → file}, post: {k → entry} }
 * /snap/<snapshot_id>/<file>          exact API response bodies
 * ```
 *
 * GET metadata is served from the snapshot whenever the manifest lists its
 * path. An analysis POST is served from it when the manifest lists
 * `k = sha256_hex(path + "?" + query + "\n" + body)` computed over the exact
 * bytes the SPA would send (the same key the edge cache uses). The manifest is
 * always consulted first; files are never probed, because the host answers a
 * missing file with the SPA's index.html and a 200.
 *
 * In development and E2E there is no snapshot (`current.json` is missing or
 * answered by the SPA fallback) and everything goes to the API. Any problem
 * reading the snapshot also falls back to the API: the snapshot can only make
 * the site faster and more available, never break it.
 */
import { API_VERSION_PREFIX, apiBaseUrl, type ApiResult } from './client'
import { SUPPORTED_API_VERSION, apiMajorVersion } from './compat'

export const SNAPSHOT_POINTER_PATH = '/snap/current.json'
const SNAPSHOT_ROOT = '/snap/'

export interface SnapshotPostEntry {
  path: string
  file: string
  analysis_key: string | null
  x_cache?: string
}

export interface Snapshot {
  id: string
  dataVersion: string
  /** The statistical implementation that computed the snapshot, when it says. */
  analyticsVersion: string | null
  /** API path (e.g. `/api/v1/meta`) → file, relative to the snapshot directory. */
  get: Record<string, string>
  /** Request hash → analysis response entry. */
  post: Record<string, SnapshotPostEntry>
}

const SNAPSHOT_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/
const SNAPSHOT_FILE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$/

let loading: Promise<Snapshot | null> | null = null

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function isSafeFile(file: unknown): file is string {
  return typeof file === 'string' && SNAPSHOT_FILE.test(file) && !file.split('/').includes('..')
}

async function fetchJson(url: string, init?: RequestInit): Promise<unknown> {
  const response = await fetch(url, { ...init, headers: { Accept: 'application/json' } })
  if (!response.ok) throw new Error(`HTTP ${response.status} for ${url}`)
  // A missing file comes back as index.html with a 200: not JSON, so it throws here.
  return (await response.json()) as unknown
}

/**
 * A file under `/snap/<snapshot_id>/`: immutable, so the browser keeps it for
 * a year. The host answers a missing file with the SPA's index.html and a 200
 * that carries the same one-year immutable Cache-Control (`_headers` matches
 * the request path), so a page that asked for a file of a snapshot that was
 * not deployed at that moment (a deploy gap; a rollback can deploy that
 * snapshot again later) would keep that HTML for a year. A body that is not
 * JSON is therefore fetched once more with `cache: 'reload'`, which bypasses
 * the cached copy and replaces it.
 */
async function fetchImmutableJson(url: string): Promise<unknown> {
  try {
    return await fetchJson(url)
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error
    return await fetchJson(url, { cache: 'reload' })
  }
}

/** Validates the pointer and manifest; null means "no usable snapshot". */
function parseSnapshot(pointer: unknown, manifest: unknown, id: string): Snapshot | null {
  if (!isRecord(pointer) || !isRecord(manifest)) return null
  if (manifest.snapshot_id !== id) return null
  const dataVersion = manifest.data_version ?? pointer.data_version
  if (typeof dataVersion !== 'string' || dataVersion === '') return null
  const analytics = manifest.analytics_version ?? pointer.analytics_version
  const analyticsVersion = typeof analytics === 'string' && analytics !== '' ? analytics : null
  if (!isRecord(manifest.get) || !isRecord(manifest.post)) return null

  const get: Record<string, string> = {}
  for (const [path, file] of Object.entries(manifest.get)) {
    if (path.startsWith('/api/') && isSafeFile(file)) get[path] = file
  }
  const post: Record<string, SnapshotPostEntry> = {}
  for (const [key, entry] of Object.entries(manifest.post)) {
    if (!/^[0-9a-f]{64}$/.test(key) || !isRecord(entry)) continue
    if (typeof entry.path !== 'string' || !isSafeFile(entry.file)) continue
    post[key] = {
      path: entry.path,
      file: entry.file,
      analysis_key: typeof entry.analysis_key === 'string' ? entry.analysis_key : null,
      x_cache: typeof entry.x_cache === 'string' ? entry.x_cache : undefined,
    }
  }
  return { id, dataVersion, analyticsVersion, get, post }
}

async function fetchSnapshot(): Promise<Snapshot | null> {
  let pointer: unknown
  try {
    pointer = await fetchJson(SNAPSHOT_POINTER_PATH, { cache: 'no-cache' })
  } catch {
    return null
  }
  if (!isRecord(pointer)) return null
  const id = pointer.snapshot_id
  if (typeof id !== 'string' || !SNAPSHOT_ID.test(id)) return null
  // A snapshot written for another API major version is not this SPA's.
  if (apiMajorVersion(pointer.api_version) !== SUPPORTED_API_VERSION) return null
  let manifest: unknown
  try {
    manifest = await fetchImmutableJson(`${SNAPSHOT_ROOT}${id}/manifest.json`)
  } catch {
    return null
  }
  return parseSnapshot(pointer, manifest, id)
}

/** The deployed snapshot, loaded once per page; null when there is none. */
export function loadSnapshot(): Promise<Snapshot | null> {
  loading ??= fetchSnapshot().catch(() => null)
  return loading
}

/** What identifies a release to the page; null where unknown (or, as a target, unchanged). */
export interface ReleaseVersions {
  dataVersion: string | null
  analyticsVersion: string | null
}

function nonEmpty(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null
}

/** The release the snapshot pointer names now, or null when it is unusable. */
async function pointerVersions(): Promise<ReleaseVersions | null> {
  let pointer: unknown
  try {
    pointer = await fetchJson(SNAPSHOT_POINTER_PATH, { cache: 'no-cache' })
  } catch {
    return null
  }
  if (!isRecord(pointer) || typeof pointer.data_version !== 'string') return null
  if (apiMajorVersion(pointer.api_version) !== SUPPORTED_API_VERSION) return null
  return {
    dataVersion: pointer.data_version,
    analyticsVersion: nonEmpty(pointer.analytics_version),
  }
}

/**
 * The release the site's own `/api/v1/meta` reports now (headers, else the
 * body), as a reload without a snapshot reads it; null when it cannot be read.
 */
async function siteMetaVersions(): Promise<ReleaseVersions | null> {
  try {
    const response = await fetch(`${apiBaseUrl()}${API_VERSION_PREFIX}/meta`, {
      cache: 'no-cache',
      headers: { Accept: 'application/json' },
    })
    if (!response.ok) return null
    const body = (await response.json()) as unknown
    const record = isRecord(body) ? body : {}
    const data = isRecord(record.data) ? record.data : {}
    return {
      dataVersion:
        nonEmpty(response.headers.get('X-Data-Version')) ??
        nonEmpty(record.data_version) ??
        nonEmpty(data.data_version),
      analyticsVersion:
        nonEmpty(response.headers.get('X-Analytics-Version')) ??
        nonEmpty(record.analytics_version),
    }
  } catch {
    return null
  }
}

/**
 * Whether reloading the page would load metadata of the release the service
 * now serves (`target`: the versions that changed; null fields are not
 * compared). A reload reads the snapshot pointer first and loads whatever
 * snapshot it names: during a release switch (ADR-013 step 8) the API serves
 * the new release before the deploy job publishes the new snapshot, and a
 * reload then loads the old one again. Without a usable pointer the reload's
 * `/meta` is the site's `/api/v1/meta`, so that is asked: through the site
 * the edge Worker answers it from its own deployed snapshot, not from the
 * API, so it can be the old release too (review round 2; it was assumed to
 * be the API's). Whatever cannot be read counts as "the reload helps".
 */
export async function reloadLoadsVersion(target: ReleaseVersions): Promise<boolean> {
  const reloaded = (await pointerVersions()) ?? (await siteMetaVersions())
  if (reloaded === null) return true
  const agrees = (wanted: string | null, found: string | null) =>
    wanted === null || found === null || wanted === found
  return (
    agrees(target.dataVersion, reloaded.dataVersion) &&
    agrees(target.analyticsVersion, reloaded.analyticsVersion)
  )
}

/** Test isolation: forget the loaded snapshot. */
export function resetSnapshot(): void {
  loading = null
}

function fileUrl(snapshot: Snapshot, file: string): string {
  return `${SNAPSHOT_ROOT}${snapshot.id}/${file}`
}

/** A GET response from the snapshot, or null (not listed, no snapshot, or unreadable). */
export async function snapshotGet<T>(apiPath: string): Promise<ApiResult<T> | null> {
  const snapshot = await loadSnapshot()
  const file = snapshot?.get[apiPath]
  if (!snapshot || file === undefined) return null
  try {
    const data = (await fetchImmutableJson(fileUrl(snapshot, file))) as T
    return { data, analysisKey: null, cache: 'snapshot', dataVersion: snapshot.dataVersion }
  } catch {
    return null
  }
}

function toHex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer), (byte) => byte.toString(16).padStart(2, '0')).join(
    '',
  )
}

/**
 * The request hash shared with the edge cache and the snapshot builder:
 * `sha256_hex(path + "?" + query + "\n" + body)` over UTF-8 bytes. Null when
 * WebCrypto is unavailable (an insecure http origin), which only means the
 * snapshot is skipped.
 */
export async function requestKey(
  path: string,
  query: string,
  body: string,
): Promise<string | null> {
  const subtle = globalThis.crypto?.subtle
  if (!subtle) return null
  const bytes = new TextEncoder().encode(`${path}?${query}\n${body}`)
  return toHex(await subtle.digest('SHA-256', bytes))
}

/**
 * An analysis response from the snapshot for exactly these body bytes, or
 * null. The analysis key shown to the user is the one the API sent when the
 * snapshot was generated (`analysis_key` in the manifest).
 */
export async function snapshotPost<T>(
  apiPath: string,
  body: string,
): Promise<ApiResult<T> | null> {
  const snapshot = await loadSnapshot()
  if (!snapshot) return null
  const key = await requestKey(apiPath, '', body)
  const entry = key === null ? undefined : snapshot.post[key]
  if (!entry || entry.path !== apiPath) return null
  try {
    const data = (await fetchImmutableJson(fileUrl(snapshot, entry.file))) as T
    return {
      data,
      analysisKey: entry.analysis_key,
      cache: 'snapshot',
      dataVersion: snapshot.dataVersion,
    }
  } catch {
    return null
  }
}
