/**
 * Correctness requirement (spec §85): a trend containing 2020 must render a
 * visible, explained gap — no connecting line, no interpolation, no zero.
 */
import { expect, test } from './fixtures'

import { CORE } from './helpers'

test('2020 renders as an explicit, explained gap in trends', CORE, async ({ page }) => {
  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()

  await page.getByRole('radio', { name: 'Change over time' }).click()
  await page.getByLabel('From', { exact: true }).selectOption('2019')
  await page.getByLabel('To', { exact: true }).selectOption('2023')
  // The builder already hints that 2020 will be a gap.
  await expect(page.getByText(/2020 will show as a gap/)).toBeVisible()

  await page.getByRole('button', { name: 'Analyze' }).click()

  // The gap band and its explanation: the API's reason, in plain language.
  await expect(page.getByTestId('unavailable-2020')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByTestId('unavailable-2020')).toContainText('No data')
  await expect(page.getByTestId('gap-note')).toContainText('No data for 2020')
  await expect(page.getByTestId('gap-note')).toContainText(
    'Data collection paused in spring 2020 because of COVID-19',
  )

  // 2019 and 2023 are single points separated by the gap: with only one
  // available year on each side there is NO line segment at all — nothing
  // can connect across 2020.
  await expect(page.getByTestId('point-2019')).toBeVisible()
  await expect(page.getByTestId('point-2023')).toBeVisible()
  expect(await page.getByTestId('trend-line-segment').count()).toBe(0)
  await expect(page.getByTestId('point-2020')).toHaveCount(0)

  // The table for screen readers represents 2020 as having no estimate —
  // never as zero.
  const row = page.getByRole('rowheader', { name: '2020', exact: true }).locator('..')
  await expect(row).toContainText('No data')
  await expect(row).not.toContainText('0m')

  // 2019 (240 min) and 2023 (75 min) show their true values.
  await expect(page.getByRole('table')).toContainText('4h 0m')
  await expect(page.getByRole('table')).toContainText('1h 15m')
})

test(
  'the chart is keyboard-inspectable, including the unavailable year',
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
    await expect(chart).toContainText('4h 0m2019')
    await page.keyboard.press('ArrowRight')
    await expect(chart).toContainText(/2020: no estimate/i)
  },
)

test('a trend split by sex draws one line per group, with a legend', CORE, async ({ page }) => {
  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()
  await page.getByRole('radio', { name: 'Change over time' }).click()
  await page.getByLabel('From', { exact: true }).selectOption('2019')
  await page.getByLabel('To', { exact: true }).selectOption('2023')
  await page.getByLabel('Lines').selectOption('sex')
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
  // 2020 is still a labeled gap, and no band is drawn for several lines.
  await expect(page.getByTestId('unavailable-2020')).toBeVisible()
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
