/**
 * Correctness requirement (spec §85): a trend containing 2020 must render a
 * visible, explained gap — no connecting line, no interpolation, no zero.
 */
import { expect, test } from '@playwright/test'

test('2020 renders as an explicit, explained gap in trends', async ({ page }) => {
  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()

  await page.getByRole('radio', { name: 'Trend over time' }).click()
  await page.getByLabel('From', { exact: true }).selectOption('2019')
  await page.getByLabel('To', { exact: true }).selectOption('2023')
  // The builder already hints that 2020 will be a gap.
  await expect(page.getByText(/2020 will appear as an explicit gap/)).toBeVisible()

  await page.getByRole('button', { name: 'Analyze' }).click()

  // The gap band and its explanation, straight from the API's reason.
  await expect(page.getByTestId('unavailable-2020')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByTestId('gap-note')).toContainText('2020 has no estimate')
  await expect(page.getByTestId('gap-note')).toContainText('TUFNWGTP is undefined for 2020')

  // 2019 and 2023 are single points separated by the gap: with only one
  // available year on each side there is NO line segment at all — nothing
  // can connect across 2020.
  await expect(page.getByTestId('point-2019')).toBeVisible()
  await expect(page.getByTestId('point-2023')).toBeVisible()
  expect(await page.getByTestId('trend-line-segment').count()).toBe(0)
  await expect(page.getByTestId('point-2020')).toHaveCount(0)

  // The data table represents 2020 as unavailable — never as zero.
  const row = page.getByRole('rowheader', { name: '2020', exact: true }).locator('..')
  await expect(row).toContainText('Unavailable')
  await expect(row).not.toContainText('0.0 min')

  // 2019 (240 min) and 2023 (75 min) show their true values.
  await expect(page.getByRole('table')).toContainText('240.0 min')
  await expect(page.getByRole('table')).toContainText('75.0 min')
})

test('the chart is keyboard-inspectable, including the unavailable year', async ({ page }) => {
  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()
  await page.getByRole('radio', { name: 'Trend over time' }).click()
  await page.getByLabel('From', { exact: true }).selectOption('2019')
  await page.getByLabel('To', { exact: true }).selectOption('2023')
  await page.getByRole('button', { name: 'Analyze' }).click()

  const chart = page.getByRole('application', { name: /Average time per day by year/ })
  await expect(chart).toBeVisible({ timeout: 15_000 })
  await chart.focus()
  await page.keyboard.press('ArrowRight')
  await expect(chart).toContainText('2019')
  await expect(chart).toContainText('240.0 min')
  await page.keyboard.press('ArrowRight')
  await expect(chart).toContainText(/2020.*[Uu]navailable|Unavailable/)
})
