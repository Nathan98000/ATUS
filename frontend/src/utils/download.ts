/**
 * Saves text the page generated as a file, without a server round trip.
 *
 * The file is a Blob behind an object URL, opened through a temporary
 * `<a download>`: no inline script, no `data:` URL and no new connection,
 * so it works under the site's strict Content-Security-Policy (a navigation
 * to a `blob:` URL of the page's own origin is not a fetch that
 * `connect-src` governs).
 */

/** How long the object URL outlives the click (some browsers read it asynchronously). */
const REVOKE_AFTER_MS = 10_000

export function downloadText(fileName: string, contents: string, mimeType: string): void {
  const url = URL.createObjectURL(new Blob([contents], { type: mimeType }))
  const link = document.createElement('a')
  link.href = url
  link.download = fileName
  link.hidden = true
  document.body.append(link)
  try {
    link.click()
  } finally {
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), REVOKE_AFTER_MS)
  }
}
