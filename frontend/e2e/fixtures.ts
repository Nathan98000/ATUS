/**
 * The `test` and `expect` every spec uses, with the two settings that let one
 * suite run in both E2E modes (playwright.config.ts):
 *
 * - **vite-preview mode** (default): the production build behind `vite
 *   preview`, whose /api proxy leads to scripts/e2e_backend.py;
 * - **stack mode** (`E2E_BASE_URL`): the production-mode stack through the
 *   edge (scripts/smoke_stack.sh --edge --e2e), where the Worker, the static
 *   snapshot and the production API's limits are all in the path.
 *
 * In stack mode each test is its own visitor. `wrangler dev` takes
 * `CF-Connecting-IP` from the request when one is sent (Miniflare sets it from
 * the socket only otherwise; Cloudflare's edge overwrites it in production),
 * so every test sends an address of its own from 198.19.0.0/16 (benchmarking
 * addresses, never routable; the edge probe uses 198.18.0.0/16). The
 * production limits stay untouched: the API's 6 new analyses a minute per
 * client and the governor's daily allowance per /24 apply per test, instead
 * of the whole suite counting as one visitor.
 */
import { test as base, type Page } from '@playwright/test'

export { expect } from '@playwright/test'

/** True when the suite runs through the production-mode stack's edge. */
export const STACK_MODE = Boolean(process.env.E2E_BASE_URL)

/** A stable address in 198.19.0.0/16 for one test run (its own /24 in most cases). */
export function visitorAddress(key: string): string {
  let hash = 0x811c9dc5
  for (const char of key) {
    hash = Math.imul(hash ^ char.charCodeAt(0), 0x01000193) >>> 0
  }
  return `198.19.${hash % 256}.${1 + ((hash >>> 8) % 254)}`
}

export const test = base.extend<{ visitor: string }>({
  visitor: [
    async ({ context }, use, testInfo) => {
      const address = visitorAddress(
        `${testInfo.project.name}|${testInfo.testId}|${testInfo.retry}|${testInfo.repeatEachIndex}`,
      )
      if (STACK_MODE) await context.setExtraHTTPHeaders({ 'CF-Connecting-IP': address })
      await use(address)
    },
    { auto: true },
  ],
})

/**
 * Makes the page ignore the static snapshot, so metadata comes from
 * `/api/v1/...` where a test intercepts it. In vite-preview mode there is no
 * snapshot anyway; in stack mode the SPA would otherwise read `/meta` from
 * `/snap/` and never call the route the test injects a failure into.
 */
export async function withoutSnapshot(page: Page): Promise<void> {
  await page.route('**/snap/current.json', (route) =>
    route.fulfill({ status: 404, contentType: 'text/plain', body: 'no snapshot' }),
  )
}
