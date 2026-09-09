/**
 * Shareable analyses (spec §84): the copied URL must actually reconstruct
 * the analysis in a fresh browser context — same configuration, same result.
 */
import { expect, test } from '@playwright/test'

import { analyze, setAge } from './helpers'

test('a shared analysis URL reproduces the analysis in a fresh browser context', async ({
  page,
  browser,
}) => {
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'])

  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()
  await setAge(page, 25, 54)
  await page.getByLabel('Year', { exact: true }).selectOption('2023')
  await analyze(page)
  await expect(page.locator('.stat-value')).toHaveText('2h 30m', { timeout: 15_000 })

  await page.getByRole('button', { name: 'Share analysis' }).click()
  await expect(page.getByText('Link copied.')).toBeVisible()
  const sharedUrl = await page.evaluate(() => navigator.clipboard.readText())
  expect(sharedUrl).toContain('/analysis/estimate?spec=')

  // A completely fresh context: new storage, new cache, nothing carried over.
  const freshContext = await browser.newContext()
  const freshPage = await freshContext.newPage()
  await freshPage.goto(sharedUrl)

  // Same result…
  await expect(freshPage.locator('.stat-value')).toHaveText('2h 30m', { timeout: 15_000 })
  await expect(freshPage.getByText('150.0 minutes per day')).toBeVisible()
  // …and the same reconstructable configuration.
  await expect(freshPage).toHaveTitle(/Sleeping — Ages 25–54 — 2023/)
  await freshPage.getByRole('link', { name: 'Adjust analysis' }).click()
  await expect(freshPage.getByLabel('Minimum age')).toHaveValue('25')
  await expect(freshPage.getByLabel('Maximum age')).toHaveValue('54')
  await expect(freshPage.getByLabel('Year', { exact: true })).toHaveValue('2023')
  await freshContext.close()
})

test('refreshing a result page reproduces the same analysis', async ({ page }) => {
  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()
  await setAge(page, 25, 54)
  await page.getByLabel('Year', { exact: true }).selectOption('2023')
  await analyze(page)
  await expect(page.locator('.stat-value')).toHaveText('2h 30m', { timeout: 15_000 })

  await page.reload()
  await expect(page.locator('.stat-value')).toHaveText('2h 30m', { timeout: 15_000 })
  await expect(page.getByText(/± 8.3 min/)).toBeVisible()
})

test('an invalid share link gets a helpful page, not a blank screen', async ({ page }) => {
  await page.goto('/analysis/estimate?spec=this-is-not-a-spec')
  await expect(page.getByRole('alert')).toContainText('This analysis link cannot be opened')
  await page.getByRole('link', { name: /Build an analysis/ }).click()
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()
})
