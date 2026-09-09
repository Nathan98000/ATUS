/**
 * Automated accessibility checks (axe) on the main screens, plus a
 * keyboard-only pass through the primary workflow. Automated tooling is a
 * floor, not a ceiling — component tests cover widget-specific semantics.
 */
import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

import { analyze, setAge } from './helpers'

async function expectNoSeriousViolations(page: import('@playwright/test').Page) {
  const results = await new AxeBuilder({ page }).analyze()
  const serious = results.violations.filter((violation) =>
    ['serious', 'critical'].includes(violation.impact ?? ''),
  )
  expect(
    serious.map((violation) => `${violation.id}: ${violation.nodes.length} nodes`),
  ).toEqual([])
}

test('home page has no serious axe violations', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: /Explore how Americans/ })).toBeVisible()
  await expectNoSeriousViolations(page)
})

test('explorer has no serious axe violations', async ({ page }) => {
  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()
  await expectNoSeriousViolations(page)
})

test('estimate, trend and compare results have no serious axe violations', async ({ page }) => {
  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()
  await setAge(page, 25, 54)
  await page.getByLabel('Year', { exact: true }).selectOption('2023')
  await analyze(page)
  await expect(page.locator('.stat-value')).toHaveText('2h 30m', { timeout: 15_000 })
  await expectNoSeriousViolations(page)

  await page.goto('/explore')
  await page.getByRole('radio', { name: 'Trend over time' }).click()
  await page.getByLabel('From', { exact: true }).selectOption('2019')
  await analyze(page)
  await expect(page.getByTestId('unavailable-2020')).toBeVisible({ timeout: 15_000 })
  await expectNoSeriousViolations(page)

  await page.goto('/explore')
  await page.getByRole('radio', { name: 'Compare two groups' }).click()
  await page.getByRole('group', { name: 'Group A' }).getByLabel('Sex').selectOption('male')
  await page.getByRole('group', { name: 'Group B' }).getByLabel('Sex').selectOption('female')
  await page.getByLabel('Year', { exact: true }).selectOption('2023')
  await analyze(page)
  await expect(page.getByTestId('difference')).toBeVisible({ timeout: 15_000 })
  await expectNoSeriousViolations(page)
})

test('the primary workflow is keyboard-operable', async ({ page }) => {
  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()

  // Open the activity picker and select via keyboard only.
  await page.getByRole('button', { name: 'Change activity' }).focus()
  await page.keyboard.press('Enter')
  const search = page.getByLabel('Search activities')
  await search.focus()
  await page.keyboard.type('sleep')
  await page.keyboard.press('Enter') // first option
  await expect(
    page.getByText('includes all 3 specific activities', { exact: false }),
  ).toBeVisible()
  // Focus returned to the toggle for a coherent tab order.
  await expect(page.getByRole('button', { name: 'Change activity' })).toBeFocused()

  // Run the analysis with the keyboard.
  await page.getByRole('button', { name: 'Analyze' }).focus()
  await page.keyboard.press('Enter')
  await expect(page.locator('.stat-value')).toHaveText('1h 15m', { timeout: 15_000 })

  // On a fresh page load, the skip link is the first tab stop.
  await page.goto('/')
  await expect(page.getByRole('heading', { name: /Explore how Americans/ })).toBeVisible()
  await page.keyboard.press('Tab')
  await expect(page.getByRole('link', { name: 'Skip to content' })).toBeFocused()
})
