/**
 * Brief §15 (ADR-013): a new survey year reaches the page through /meta
 * alone. /meta is intercepted to announce a future release — code 0326, its
 * own data version, the fixture database's years plus 2026 — and everything
 * else is the real API on the fixture database (2019, 2020 and 2023), its
 * responses tagged with the same data version, as a 0326 deployment's are.
 */
import type { Page } from '@playwright/test'

import { expect, test, withoutSnapshot } from './fixtures'
import { analyze } from './helpers'

const DATA_VERSION_0326 = '0326:serving-20270915T120000Z-e2e00326:00112233aabb'

/** Serves the API as a 0326 deployment would: /meta announces 2026. */
async function announce0326(page: Page): Promise<void> {
  await withoutSnapshot(page)
  await page.route('**/api/v1/**', async (route) => {
    const response = await route.fetch()
    const headers = response.headers()
    if ('x-data-version' in headers) headers['x-data-version'] = DATA_VERSION_0326
    if (new URL(route.request().url()).pathname !== '/api/v1/meta') {
      await route.fulfill({ response, headers })
      return
    }
    const meta = await response.json()
    delete headers['content-length']
    delete headers['content-encoding']
    await route.fulfill({
      response,
      headers,
      json: {
        ...meta,
        data: { ...meta.data, release: '0326', years: [...meta.data.years, 2026] },
      },
    })
  })
}

/** The spec of an in-app analysis link (the app's base64url encoding). */
function specOf(href: string | null): { years: number[] } {
  const encoded = new URLSearchParams((href ?? '').split('?')[1]).get('spec') ?? ''
  return JSON.parse(Buffer.from(encoded, 'base64url').toString('utf-8'))
}

test('a release announcing 2026 reaches every year control, and 2020 is still a gap', async ({
  page,
}) => {
  await announce0326(page)
  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible({
    timeout: 15_000,
  })
  await expect(page.getByRole('contentinfo')).toContainText('Data release 0326 (2019–2026)')

  // The latest reported year is the default, newest first.
  const year = page.getByLabel('Year', { exact: true })
  await expect(year).toHaveValue('2026')
  await expect(year.locator('option')).toHaveText(['2026', '2023', '2020', '2019'])

  // The default estimate asks the API for 2026, and the API, not the page,
  // decides: this fixture database has no 2026, so it refuses the year.
  const sent = page.waitForRequest('**/api/v1/analysis/estimate')
  await analyze(page)
  expect((await sent).postDataJSON().years).toEqual([2026])
  await expect(page.getByRole('heading', { level: 1 })).toContainText('2026')
  await expect(page.getByRole('alert')).toContainText('2026', { timeout: 15_000 })

  // A trend defaults to the full reported period, which now ends in 2026.
  await page.goto('/explore')
  await page.getByRole('radio', { name: 'Trend over time' }).click()
  const from = page.getByLabel('From', { exact: true })
  const to = page.getByLabel('To', { exact: true })
  await expect(from).toHaveValue('2019')
  await expect(to).toHaveValue('2026')
  await expect(to.locator('option')).toHaveText(['2019', '2020', '2023', '2026'])
  await expect(page.getByText(/2020 will appear as an explicit gap/)).toBeVisible()

  // 2020's rules hold in the real API's answer under the new release.
  await to.selectOption('2023')
  await analyze(page)
  await expect(page.getByTestId('unavailable-2020')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByTestId('gap-note')).toContainText('TUFNWGTP is undefined for 2020')
  await expect(page.getByTestId('point-2019')).toBeVisible()
  await expect(page.getByTestId('point-2023')).toBeVisible()
  expect(await page.getByTestId('trend-line-segment').count()).toBe(0)
  // Page and results agree on the release: no reload notice.
  await expect(
    page.getByRole('status').filter({ hasText: 'different data release' }),
  ).toHaveCount(0)

  // The home page's starters ask for 2026, and the full-period trend for every year.
  await page.goto('/')
  const sleep = page.getByRole('link', { name: /How much do Americans sleep\?/ })
  await expect(sleep).toBeVisible({ timeout: 15_000 })
  expect(specOf(await sleep.getAttribute('href')).years).toEqual([2026])
  const leisure = page.getByRole('link', { name: /How has leisure time changed\?/ })
  expect(specOf(await leisure.getAttribute('href')).years).toEqual([2019, 2020, 2023, 2026])
})
