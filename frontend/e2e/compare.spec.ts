/**
 * The comparison scenario (spec §139): two groups, API-computed difference,
 * methodology, and a reproducible share link.
 */
import { expect, test } from '@playwright/test'

test('configure, run, understand and reshare a comparison', async ({ page }) => {
  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()

  await page.getByRole('radio', { name: 'Compare two groups' }).click()

  const groupA = page.getByRole('group', { name: 'Group A' })
  const groupB = page.getByRole('group', { name: 'Group B' })
  await groupA.getByLabel('Name').fill('Men')
  await groupA.getByLabel('Sex').selectOption('male')
  await groupB.getByLabel('Name').fill('Women')
  await groupB.getByLabel('Sex').selectOption('female')
  await page.getByLabel('Year', { exact: true }).selectOption('2023')

  await page.getByRole('button', { name: 'Analyze' }).click()

  // Fixture: men 100.0 min (1h 40m), women 66.67 min (1h 7m).
  await expect(page.locator('.compare-card__value').first()).toHaveText('1h 40m', {
    timeout: 15_000,
  })
  await expect(page.locator('.compare-card__value').nth(1)).toHaveText('1h 7m')

  // The difference comes from the API (covariance-correct), labeled A − B.
  const difference = page.getByTestId('difference')
  await expect(difference).toContainText('Men − Women')
  await expect(difference).toContainText('+33.3 min')
  await expect(difference).toContainText('± 11.8 min')
  await expect(difference).toContainText('A positive difference means “Men”')

  // Both groups appear in the chart with their labels (never color-only).
  await expect(page.getByRole('img', { name: /Comparison of minutes per day/ })).toBeVisible()

  // Methodology explains the difference semantics.
  await page.getByText('How was this calculated?').click()
  await expect(page.locator('dd', { hasText: 'per-replicate differences' })).toBeVisible()

  // Reloading the URL reproduces the comparison (stateless share link).
  await page.reload()
  await expect(page.locator('.compare-card__value').first()).toHaveText('1h 40m', {
    timeout: 15_000,
  })
  await expect(page.getByTestId('difference')).toContainText('+33.3 min')
})
