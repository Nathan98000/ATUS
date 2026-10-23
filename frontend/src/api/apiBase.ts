/**
 * Where the API lives, decided once at build time.
 *
 * The public deployment serves the SPA and proxies `/api/*` from one origin
 * (ADR-011), so the default is the empty base: requests go to the page's own
 * origin as relative `/api/v1/...` URLs. A separate API origin is opt-in, by
 * setting `VITE_API_BASE_URL` to an absolute http(s) URL (local E2E runs do
 * this). Nothing ever falls back to a developer address such as
 * `http://localhost:8000` (audit F24).
 *
 * This module has no imports so that `vite.config.ts` can apply the same
 * rule when it validates a production build's configuration.
 */

/** True for an absolute `http:` or `https:` URL. */
export function isAbsoluteHttpUrl(value: string): boolean {
  let url: URL
  try {
    url = new URL(value)
  } catch {
    return false
  }
  return url.protocol === 'http:' || url.protocol === 'https:'
}

/**
 * The API base for a configured value: '' (same origin) unless the value is
 * an absolute http(s) URL, which is returned without trailing slashes.
 * The build rejects any other non-empty value, so '' here never hides a typo
 * in a production bundle.
 */
export function resolveApiBase(configured: string | undefined): string {
  const value = configured?.trim() ?? ''
  if (!isAbsoluteHttpUrl(value)) return ''
  return value.replace(/\/+$/, '')
}

/**
 * Build-time check of `VITE_API_BASE_URL`: unset/empty (same origin) or an
 * absolute http(s) URL. Returns an error message, or null when acceptable.
 */
export function apiBaseConfigError(configured: string | undefined): string | null {
  const value = configured?.trim() ?? ''
  if (value === '' || isAbsoluteHttpUrl(value)) return null
  return (
    `VITE_API_BASE_URL must be unset (same-origin /api/v1) or an absolute http(s) URL; ` +
    `got ${JSON.stringify(value)}.`
  )
}
