/**
 * Automated accessibility checks (axe) on the main screens, plus a
 * keyboard-only pass through the primary workflow. Automated tooling is a
 * floor, not a ceiling — component tests cover widget-specific semantics.
 */
import { expect, test } from './fixtures'

import { analyze, expectNoSeriousViolations, setAge } from './helpers'

test('home page has no serious axe violations', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: /Explore how Americans/ })).toBeVisible()
  await expectNoSeriousViolations(page)
})

test('explorer has no serious axe violations', async ({ page }) => {
  await page.goto('/explore')
  // The builder itself: its heading is there in the loading state too.
  await expect(page.getByRole('button', { name: 'Analyze' })).toBeVisible()
  await expectNoSeriousViolations(page)
})

test('the About page, with its privacy and logging statement, has no serious axe violations', async ({
  page,
}) => {
  await page.goto('/about')
  await expect(page.getByRole('heading', { name: 'Privacy and logs' })).toBeVisible()
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
  await page.getByRole('radio', { name: 'Change over time' }).click()
  await page.getByLabel('From', { exact: true }).selectOption('2019')
  await analyze(page)
  await expect(page.getByTestId('point-2020')).toBeVisible({ timeout: 15_000 })
  await expectNoSeriousViolations(page)

  await page.goto('/explore')
  await page.getByRole('radio', { name: 'Compare two groups' }).click()
  // Men vs women, the default comparison.
  await page.getByLabel('Year', { exact: true }).selectOption('2023')
  await analyze(page)
  await expect(page.getByTestId('difference')).toBeVisible({ timeout: 15_000 })
  await expectNoSeriousViolations(page)
})

test('the primary workflow is keyboard-operable', async ({ page }) => {
  // In-app navigation moves focus to the new page's heading, so the change of
  // page is announced and Tab continues from it (review round 2: focus
  // stayed on <body>).
  await page.goto('/')
  await page.getByRole('link', { name: 'Make your own chart' }).focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { level: 1, name: 'Build an analysis' })).toBeFocused()
  await expect(page.getByRole('button', { name: 'Analyze' })).toBeVisible()

  // Open the activity picker and select via keyboard only.
  await page.getByRole('button', { name: 'Change activity' }).focus()
  await page.keyboard.press('Enter')
  const search = page.getByLabel('Search activities')
  await search.focus()
  await page.keyboard.type('sleep')
  await page.keyboard.press('Enter') // first option
  await expect(
    page.getByText('includes all 2 activities under it', { exact: false }),
  ).toBeVisible()
  // Focus returned to the toggle for a coherent tab order.
  await expect(page.getByRole('button', { name: 'Change activity' })).toBeFocused()

  // Run the analysis with the keyboard: focus lands on the result's heading
  // and stays there when the URL is rewritten to the canonical spec.
  await page.getByRole('button', { name: 'Analyze' }).focus()
  await page.keyboard.press('Enter')
  const resultHeading = page.getByRole('heading', { level: 1 })
  await expect(resultHeading).toBeFocused()
  await expect(resultHeading).toContainText('Sleeping')
  await expect(page.locator('.stat-value')).toHaveText('1h 15m', { timeout: 15_000 })
  await expect(resultHeading).toBeFocused()

  // On a fresh page load, the skip link is the first tab stop.
  await page.goto('/')
  await expect(page.getByRole('heading', { name: /Explore how Americans/ })).toBeVisible()
  await page.keyboard.press('Tab')
  await expect(page.getByRole('link', { name: 'Skip to content' })).toBeFocused()
})
