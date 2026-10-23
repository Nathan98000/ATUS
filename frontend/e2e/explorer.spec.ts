/**
 * The primary end-to-end scenario: home → explore → search activity →
 * population → year → statistic → run → understand result (spec §138).
 */
import { expect, test } from './fixtures'

import {
  CORE,
  analysisUrl,
  analyze,
  chooseOneNumber,
  pickSleepBySearch,
  setAge,
} from './helpers'

test('a first-time user completes an estimate from the home page', CORE, async ({ page }) => {
  await page.goto('/')
  await expect(page).toHaveTitle('ATUS Explorer')
  await expect(
    page.getByRole('heading', { name: /Explore how Americans spend their time/ }),
  ).toBeVisible()

  await page.getByRole('link', { name: 'Make your own chart' }).click()
  await expect(page.getByRole('heading', { name: 'Make a chart' })).toBeVisible()
  // The builder opens on a trend; this visitor wants one number.
  await chooseOneNumber(page)

  // The activity lexicon is discovered from the API and searchable.
  await pickSleepBySearch(page)

  // Population: ages 25–54 (fixture: two respondents qualify).
  await setAge(page, 25, 54)

  // Years come from /meta (fixture data release has 2019, 2020, 2023).
  await page.getByLabel('Year', { exact: true }).selectOption('2023')

  // Statistic: default "Average time per day" is already selected.
  await expect(page.getByLabel('Show', { exact: true })).toHaveValue('average_minutes_per_day')

  await analyze(page)

  // Result: 150.0 minutes = 2h 30m, SE 8.33, n=2 (hand-computable).
  await expect(page.locator('.stat-value')).toHaveText('2h 30m')
  await expect(page.getByText('per day, on average')).toBeVisible()
  // The interval 133.7 to 166.3 minutes, as clock times.
  await expect(page.getByText(/95% confidence interval:/)).toContainText('2h 14m to 2h 46m')
  // The figure puts it in proportion: a stretch of one 24-hour day.
  await expect(page.getByRole('img', { name: '2h 30m out of 24 hours' })).toBeVisible()
  // The small print names the sample on its own line, and the source.
  await expect(page.getByText("Based on 2 people's diaries.")).toBeVisible()
  await expect(page.getByText('How was this calculated?')).toHaveCount(0)

  // The URL is shareable and the title describes the analysis.
  expect(page.url()).toContain('/analysis/estimate?spec=')
  await expect(page).toHaveTitle(/Sleeping — Ages 25–54 — 2023/)
})

test('the loading state announces the calculation', async ({ page }) => {
  // Delay the response enough to observe the loading state: the API's, and in
  // stack mode the static snapshot's, which holds this default analysis (read
  // in milliseconds, the state could vanish before the first check).
  for (const url of ['**/api/v1/analysis/estimate', '**/snap/*/post/*.json']) {
    await page.route(url, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 600))
      await route.continue()
    })
  }
  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'Make a chart' })).toBeVisible()
  await chooseOneNumber(page)
  await analyze(page)
  await expect(page.getByText('Working out the number…').first()).toBeVisible()
  await expect(page.locator('.stat-value')).toBeVisible({ timeout: 15_000 })
})

test('an estimate can be viewed as a trend across all available years', async ({ page }) => {
  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'Make a chart' })).toBeVisible()
  await chooseOneNumber(page)
  await analyze(page)
  await expect(page.locator('.stat-value')).toBeVisible({ timeout: 15_000 })

  await page.getByRole('link', { name: /View .* trend/ }).click()
  // Fixture years 2019–2023: 2019 and 2023 estimated, 2020 its partial-year point.
  await expect(page.getByTestId('point-2020')).toHaveClass(/is-partial/, { timeout: 15_000 })
  await expect(page.getByTestId('point-2019')).toBeVisible()
  await expect(page.getByTestId('point-2023')).toBeVisible()
})

test('"What does 95% mean?" opens the About page at its explanation (review round 6)', async ({
  page,
}) => {
  await page.goto(
    analysisUrl('estimate', {
      measure: 'average_minutes_per_day',
      activity: { preset: 'sleep' },
      years: [2023],
    }),
  )
  await page.getByRole('link', { name: 'What does 95% mean?' }).click()
  const heading = page.getByRole('heading', { name: 'How sure are the numbers?' })
  await expect(heading).toBeFocused()
  await expect(heading).toBeInViewport()
})
