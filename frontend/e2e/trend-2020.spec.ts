/**
 * 2020 in a trend. The multi-year weights have no 2020 (collection paused
 * March 18 – May 9, 2020), so the page draws 2020 from its partial-year
 * estimate under the pandemic weights: a hollow point reached by a dotted
 * connector, with a note, never an interpolated or zero value. A year with no estimate at all would still be
 * a labeled gap (src/pages/AnalysisPage.test.tsx covers that fallback).
 */
import { expect, test } from './fixtures'

import { CORE } from './helpers'

test(
  '2020 renders as a hollow partial-year point, explained in the small print',
  CORE,
  async ({ page }) => {
    await page.goto('/explore')
    await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()

    await page.getByRole('radio', { name: 'Change over time' }).click()
    await page.getByLabel('From', { exact: true }).selectOption('2019')
    await page.getByLabel('To', { exact: true }).selectOption('2023')
    // The chart says what 2020 is; the builder no longer does (review round 4).
    await expect(page.getByText(/2020 is a partial-year estimate/)).toHaveCount(0)

    const partial = page.waitForRequest(
      (request) =>
        request.url().endsWith('/api/v1/analysis/estimate') &&
        request.postDataJSON()?.weights === 'pandemic',
    )
    await page.getByRole('button', { name: 'Analyze' }).click()
    // 2020 alone, under the pandemic weights.
    expect((await partial).postDataJSON()).toMatchObject({ years: [2020], weights: 'pandemic' })

    // The hollow point, on the line, and its explanation in plain language.
    await expect(page.getByTestId('point-2020')).toHaveClass(/is-partial/, { timeout: 15_000 })
    await expect(page.getByTestId('unavailable-2020')).toHaveCount(0)
    await expect(page.getByTestId('partial-note')).toContainText(
      '2020 (hollow point) is a partial year: the survey paused from March 18 to May 9 because of COVID-19',
    )
    // 2019 reaches 2020 by a dotted connector, not the line itself; 2023 is
    // alone after the years this fixture release does not have.
    await expect(page.getByTestId('point-2019')).toBeVisible()
    await expect(page.getByTestId('point-2023')).toBeVisible()
    expect(await page.getByTestId('trend-line-connector').count()).toBe(1)
    expect(await page.getByTestId('trend-line-segment').count()).toBe(0)

    // The table for screen readers names 2020 a partial year, with its value:
    // 2019 (240 min), 2020 (240 min, pandemic weights) and 2023 (75 min).
    const row = page.getByRole('rowheader', { name: '2020 (partial year)' }).locator('..')
    await expect(row).toContainText('4h')
    await expect(page.getByRole('table')).toContainText('1h 15m')
  },
)

test(
  'the chart is keyboard-inspectable, including the partial year',
  CORE,
  async ({ page }) => {
    await page.goto('/explore')
    await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()
    await page.getByRole('radio', { name: 'Change over time' }).click()
    await page.getByLabel('From', { exact: true }).selectOption('2019')
    await page.getByLabel('To', { exact: true }).selectOption('2023')
    await page.getByRole('button', { name: 'Analyze' }).click()

    const chart = page.getByRole('application', { name: /Average time per day by year/ })
    await expect(chart).toBeVisible({ timeout: 15_000 })
    // The pointer is still where "Analyze" was; if the chart renders under it,
    // a hover sets the readout's year (WebKit fires mousemove on layout), and
    // the first ArrowRight would then step from that year, not into 2019.
    // A keyboard user's pointer is elsewhere.
    await page.mouse.move(0, 0)
    await chart.press('Escape')
    await chart.focus()
    await page.keyboard.press('ArrowRight')
    // The readout: the value, then the year (240 minutes).
    await expect(chart).toContainText('4h2019')
    await page.keyboard.press('ArrowRight')
    await expect(chart).toContainText('2020 (partial year)')
  },
)

test('a trend split by sex draws one line per group, with a legend', CORE, async ({ page }) => {
  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()
  await page.getByRole('radio', { name: 'Change over time' }).click()
  await page.getByLabel('From', { exact: true }).selectOption('2019')
  await page.getByLabel('To', { exact: true }).selectOption('2023')
  await page.getByLabel('Compare by').selectOption('sex')
  await expect(page.getByRole('checkbox', { name: 'Men', exact: true })).toBeChecked()
  await expect(page.getByRole('checkbox', { name: 'Women' })).toBeChecked()

  // One point-estimate trend per group, each with its group's filter.
  const sent: unknown[] = []
  page.on('request', (request) => {
    if (request.url().endsWith('/api/v1/analysis/trend')) sent.push(request.postDataJSON())
  })
  await page.getByRole('button', { name: 'Analyze' }).click()

  await expect(page.locator('.chart-legend')).toContainText('Sex', { timeout: 15_000 })
  await expect(page.locator('.chart-legend')).toContainText('Men')
  await expect(page.locator('.chart-legend')).toContainText('Women')
  await expect(page.getByTestId('series-0')).toBeAttached()
  await expect(page.getByTestId('series-1')).toBeAttached()
  // Each group's 2020 is its partial-year estimate where it has one (this
  // fixture has a 2020 diary for a woman only: the men's line breaks there).
  await expect(page.getByTestId('partial-note')).toBeVisible()
  await expect(page.getByTestId('ci-band')).toHaveCount(0)
  expect(page.url()).toContain('&split=sex&groups=male,female')
  for (const body of sent as { population: object; variance: string }[]) {
    expect(body.variance).toBe('none')
  }
  // In stack mode the static snapshot may answer instead of the API.
  if (sent.length > 0) {
    expect(sent.map((body) => (body as { population: object }).population)).toEqual([
      { sex: 'male' },
      { sex: 'female' },
    ])
  }
})
