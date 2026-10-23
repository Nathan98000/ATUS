/**
 * Post-deploy browser smoke (playwright.deployed.config.ts): a visitor opens
 * the home page and follows a starter link, and a result renders. Works on
 * any release: it checks that an estimate is shown, not its value (the
 * statistics are checked by scripts/ops/deploy_smoke.py and
 * scripts/replay_deployed.py). On the deployed site the starter comes from
 * the static snapshot, so the journey sends at most a handful of /api
 * requests; there must be no CSP or Permissions-Policy violation.
 */
import { expect, test } from '@playwright/test'

import { collectPolicyErrors } from '../helpers'

/** /api requests a visitor's first page view may send (the smoke's budget). */
const MAX_API_REQUESTS = 6

test('a home-page starter analysis renders on the deployed site', async ({ page }) => {
  const policyErrors = await collectPolicyErrors(page)
  const apiRequests: string[] = []
  page.on('request', (request) => {
    const { pathname } = new URL(request.url())
    if (pathname === '/api' || pathname.startsWith('/api/')) apiRequests.push(pathname)
  })

  await page.goto('/')
  await expect(page).toHaveTitle('ATUS Explorer')
  await page.getByRole('link', { name: 'How much do Americans sleep?' }).click()

  await expect(page).toHaveURL(/\/analysis\/estimate\?spec=/)
  // A cold origin can take about a minute to wake; the snapshot answers at once.
  await expect(page.locator('.stat-value')).toBeVisible({ timeout: 180_000 })
  await expect(page.locator('.stat-value')).not.toHaveText('')
  // The sleep starter is a duration: its caption (since the 2026-10-22 design
  // review the page says "per day, on average", not "… minutes per day").
  await expect(page.getByText('per day, on average')).toBeVisible()

  expect(policyErrors).toEqual([])
  expect(apiRequests.length).toBeLessThanOrEqual(MAX_API_REQUESTS)
})
