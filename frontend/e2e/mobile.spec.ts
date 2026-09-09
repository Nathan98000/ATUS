/**
 * Mobile viewport (Pixel 7): the core workflow must remain usable —
 * stacked controls, no horizontal page scrolling, readable results.
 */
import { expect, test } from '@playwright/test'

test('the core workflow works on a phone without horizontal scrolling', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: /Explore how Americans/ })).toBeVisible()

  const noHorizontalScroll = async () => {
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    )
    expect(overflow).toBeLessThanOrEqual(1)
  }
  await noHorizontalScroll()

  await page.getByRole('link', { name: 'Build your own analysis' }).click()
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()
  await noHorizontalScroll()

  await page.getByLabel('Year', { exact: true }).selectOption('2023')
  await page.getByRole('button', { name: 'Analyze' }).click()
  await expect(page.locator('.stat-value')).toHaveText('1h 15m', { timeout: 15_000 })
  await noHorizontalScroll()

  // The trend chart scales to the viewport.
  await page.goto('/explore')
  await page.getByRole('radio', { name: 'Trend over time' }).click()
  await page.getByLabel('From', { exact: true }).selectOption('2019')
  await page.getByRole('button', { name: 'Analyze' }).click()
  await expect(page.getByTestId('unavailable-2020')).toBeVisible({ timeout: 15_000 })
  await noHorizontalScroll()

  // Primary actions stay visible on small screens.
  await expect(page.getByRole('button', { name: 'Share analysis' })).toBeVisible()
})

test('comparison groups stack vertically and stay readable', async ({ page }) => {
  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()
  await page.getByRole('radio', { name: 'Compare two groups' }).click()

  const groupA = page.getByRole('group', { name: 'Group A' })
  const groupB = page.getByRole('group', { name: 'Group B' })
  await expect(groupA).toBeVisible()
  const boxA = await groupA.boundingBox()
  const boxB = await groupB.boundingBox()
  // Stacked, not side by side.
  expect(boxB?.y ?? 0).toBeGreaterThan((boxA?.y ?? 0) + (boxA?.height ?? 0) - 1)

  await groupA.getByLabel('Sex').selectOption('male')
  await groupB.getByLabel('Sex').selectOption('female')
  await page.getByLabel('Year', { exact: true }).selectOption('2023')
  await page.getByRole('button', { name: 'Analyze' }).click()
  await expect(page.getByTestId('difference')).toBeVisible({ timeout: 15_000 })
})
