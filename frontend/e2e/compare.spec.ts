/**
 * The comparison scenario (spec §139): two groups, API-computed difference,
 * the chart's small print, and a reproducible share link.
 */
import { expect, test } from './fixtures'

import { CORE } from './helpers'

test('configure, run, understand and reshare a comparison', CORE, async ({ page }) => {
  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()

  await page.getByRole('radio', { name: 'Compare two groups' }).click()

  // One choice and two values: men vs women is where a comparison starts.
  await expect(page.getByLabel('Compare by')).toHaveValue('sex')
  await expect(page.getByLabel('First group')).toHaveValue('male')
  await expect(page.getByLabel('Second group')).toHaveValue('female')
  await page.getByLabel('Year', { exact: true }).selectOption('2023')

  await page.getByRole('button', { name: 'Analyze' }).click()

  // Fixture: men 100.0 min (1h 40m), women 66.67 min (1h 7m), beside their bars.
  const values = page.locator('.compare-chart__value')
  await expect(values.first()).toHaveText('1h 40m', { timeout: 15_000 })
  await expect(values.nth(1)).toHaveText('1h 7m')

  // The answer names the groups as people (review round 4).
  await expect(
    page.getByText('In 2023, men spent 33 minutes more a day sleeping than women.'),
  ).toBeVisible()
  // The difference comes from the API (covariance-correct), in words.
  const difference = page.getByTestId('difference')
  await expect(difference).toContainText('Difference: 33 minutes more per day for men.')
  await expect(difference).toContainText(
    /Likely range: [\d.]+ to [\d.]+ minutes more for men \(95% confidence\)/,
  )

  // Both groups appear in the chart with their labels (never color-only).
  await expect(page.getByRole('img', { name: /Comparison of minutes per day/ })).toBeVisible()

  // The small print explains the difference's uncertainty.
  await expect(page.getByText(/both groups coming from the same survey/)).toBeVisible()

  // Reloading the URL reproduces the comparison (stateless share link).
  await page.reload()
  await expect(values.first()).toHaveText('1h 40m', { timeout: 15_000 })
  await expect(page.getByTestId('difference')).toContainText('33 minutes more per day for men')
})
