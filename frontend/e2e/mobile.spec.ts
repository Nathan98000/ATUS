/**
 * Mobile viewport (Pixel 7): the core workflow must remain usable —
 * stacked controls, no horizontal page scrolling, readable results.
 */
import type { Page } from '@playwright/test'

import { expect, test } from './fixtures'
import { analysisUrl } from './helpers'

async function expectNoHorizontalScroll(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  )
  expect(overflow).toBeLessThanOrEqual(1)
}

test('the core workflow works on a phone without horizontal scrolling', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: /Explore how Americans/ })).toBeVisible()

  const noHorizontalScroll = () => expectNoHorizontalScroll(page)
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

test('long labels without spaces wrap instead of widening the page (review round 1)', async ({
  page,
}) => {
  // Within the API's limits: activity.label up to 120 characters, label_a
  // and label_b up to 80, any characters — a share link can carry them.
  const label120 = 'W'.repeat(120)
  const activity = { include: ['010101'], label: label120 }

  await page.goto(analysisUrl('estimate', { activity, years: [2023] }))
  await expect(page.locator('.stat-value')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('heading', { level: 1 })).toContainText(label120)
  await expectNoHorizontalScroll(page)

  await page.goto(analysisUrl('trend', { activity, years: [2019, 2020, 2023] }))
  await expect(page.getByTestId('unavailable-2020')).toBeVisible({ timeout: 15_000 })
  await expectNoHorizontalScroll(page)

  await page.goto(
    analysisUrl('compare', {
      activity: { preset: 'sleep' },
      years: [2023],
      group_a: { sex: 'male' },
      group_b: { sex: 'female' },
      label_a: 'M'.repeat(80),
      label_b: 'F'.repeat(80),
    }),
  )
  await expect(page.getByTestId('difference')).toBeVisible({ timeout: 15_000 })
  await expectNoHorizontalScroll(page)
})
