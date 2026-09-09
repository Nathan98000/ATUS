/**
 * The primary end-to-end scenario: home → explore → search activity →
 * population → year → statistic → run → understand result (spec §138).
 */
import { expect, test } from '@playwright/test'

import { analyze, pickSleepBySearch, setAge } from './helpers'

test('a first-time user completes an estimate from the home page', async ({ page }) => {
  await page.goto('/')
  await expect(page).toHaveTitle('ATUS Explorer')
  await expect(
    page.getByRole('heading', { name: /Explore how Americans spend their time/ }),
  ).toBeVisible()

  await page.getByRole('link', { name: 'Build your own analysis' }).click()
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()

  // The activity lexicon is discovered from the API and searchable.
  await pickSleepBySearch(page)

  // Population: ages 25–54 (fixture: two respondents qualify).
  await setAge(page, 25, 54)

  // Years come from /meta (fixture data release has 2019, 2020, 2023).
  await page.getByLabel('Year', { exact: true }).selectOption('2023')

  // Statistic: default "Average time per day" is already selected.
  await expect(page.getByRole('radio', { name: /Average time per day/ })).toBeChecked()

  await analyze(page)

  // Result: 150.0 minutes = 2h 30m, SE 8.33, n=2 (hand-computable).
  await expect(page.locator('.stat-value')).toHaveText('2h 30m')
  await expect(page.getByText('150.0 minutes per day')).toBeVisible()
  await expect(page.getByText(/± 8.3 min/)).toBeVisible()
  await expect(page.getByText(/2 respondents/)).toBeVisible()
  // Sample vs represented population are separate statements.
  await expect(page.getByText('Survey sample:', { exact: false })).toBeVisible()
  await expect(page.getByText('Represents:', { exact: false })).toBeVisible()

  // Methodology answers "how was this calculated?" from the response.
  await page.getByText('How was this calculated?').click()
  await expect(page.getByText(/TUFNWGTP \(multiyear\)/)).toBeVisible()
  await expect(page.getByText(/160 replicate weights/)).toBeVisible()

  // The URL is shareable and the title describes the analysis.
  expect(page.url()).toContain('/analysis/estimate?spec=')
  await expect(page).toHaveTitle(/Sleeping — Ages 25–54 — 2023/)
})

test('the loading state announces the calculation', async ({ page }) => {
  // Delay the API response enough to observe the loading state.
  await page.route('**/api/v1/analysis/estimate', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 600))
    await route.continue()
  })
  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()
  await analyze(page)
  await expect(page.getByText('Calculating estimate…')).toBeVisible()
  await expect(page.getByText(/minutes per day/).first()).toBeVisible({ timeout: 15_000 })
})

test('an estimate can be viewed as a trend across all available years', async ({ page }) => {
  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()
  await analyze(page)
  await expect(page.getByText(/minutes per day/).first()).toBeVisible({ timeout: 15_000 })

  await page.getByRole('link', { name: /View .* trend/ }).click()
  // Fixture years 2019–2023: 2019 and 2023 estimated, 2020 an explicit gap.
  await expect(page.getByTestId('unavailable-2020')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByTestId('point-2019')).toBeVisible()
  await expect(page.getByTestId('point-2023')).toBeVisible()
})
