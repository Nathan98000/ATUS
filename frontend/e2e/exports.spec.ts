/**
 * Result exports in a real browser (brief §14, §17): the CSV and JSON files
 * a visitor downloads, read back from disk. Expected numbers come from the
 * hand-computed fixture database (see helpers.ts); every CSV number must
 * equal the API's own value in the JSON download — nothing recomputed.
 */
import type { Page } from '@playwright/test'

import { expect, test } from './fixtures'
import {
  CORE,
  analysisUrl,
  csvRecords,
  downloadedText,
  expectNoSeriousViolations,
  tabReachesAllControls,
  tabTo,
} from './helpers'

async function save(page: Page, format: 'CSV' | 'JSON') {
  const downloading = page.waitForEvent('download')
  await page.getByRole('button', { name: `Download ${format}` }).click()
  const download = await downloading
  return { name: download.suggestedFilename(), text: await downloadedText(download) }
}

test(
  'an estimate downloads as CSV and JSON with the exact API values',
  CORE,
  async ({ page }) => {
    await page.goto(
      analysisUrl('estimate', {
        activity: { preset: 'sleep' },
        years: [2023],
        population: { age_min: 25, age_max: 54 },
      }),
    )
    await expect(page.locator('.stat-value')).toHaveText('2h 30m', { timeout: 15_000 })

    const csv = await save(page, 'CSV')
    expect(csv.name).toBe('atus-estimate-sleeping-2023.csv')
    const [row, ...rest] = csvRecords(csv.text)
    expect(rest).toEqual([])
    expect(row).toMatchObject({
      activity: 'Sleeping',
      years: '2023',
      measure: 'average_minutes_per_day',
      unit: 'minutes_per_day',
      estimate: '150',
      confidence_level: '0.95',
      n_respondents: '2',
      weights: 'multiyear',
      weight_variable: 'TUFNWGTP',
      variance_method: 'replicate',
      source: 'U.S. Bureau of Labor Statistics, American Time Use Survey (ATUS)',
    })
    await expect(page.getByText(`Download started: ${csv.name}`)).toBeVisible()

    const json = await save(page, 'JSON')
    expect(json.name).toBe('atus-estimate-sleeping-2023.json')
    const record = JSON.parse(json.text)
    const { estimate } = record.response
    expect(row?.estimate).toBe(String(estimate.value))
    expect(row?.standard_error).toBe(String(estimate.standard_error))
    expect(row?.ci_lower).toBe(String(estimate.ci_lower))
    expect(row?.ci_upper).toBe(String(estimate.ci_upper))
    expect(record).toMatchObject({
      operation: 'estimate',
      link: page.url(),
      spec_source: 'api_canonical',
      api_version: 'v1',
      unit: 'minutes_per_day',
      attribution: { source_url: 'https://www.bls.gov/tus/' },
    })
    expect(record.spec).toEqual(record.response.spec)
    expect(record.analytics_version).toBe(record.response.analytics_version)
    expect(record.data_version).toEqual(expect.any(String))
    expect(row?.data_version).toBe(record.data_version)
    expect(record.warnings).toEqual(record.response.warnings)

    // The analysis key is the one the methodology panel shows.
    await page.getByText('How was this calculated?').click()
    await expect(page.locator('dd code')).toHaveText(record.analysis_key)
  },
)

test('a trend CSV writes NA for the unavailable 2020, never zero', CORE, async ({ page }) => {
  await page.goto(
    analysisUrl('trend', { activity: { preset: 'sleep' }, years: [2019, 2020, 2023] }),
  )
  await expect(page.getByTestId('unavailable-2020')).toBeVisible({ timeout: 15_000 })

  const csv = await save(page, 'CSV')
  expect(csv.name).toBe('atus-trend-sleeping-2019-2023.csv')
  const rows = csvRecords(csv.text)
  expect(rows.map((row) => row.year)).toEqual(['2019', '2020', '2023'])
  expect(rows[0]).toMatchObject({ estimate: '240', unit: 'minutes_per_day' })
  expect(rows[2]).toMatchObject({ estimate: '75', unit: 'minutes_per_day' })
  expect(rows[1]).toMatchObject({
    unit: 'minutes_per_day',
    estimate: 'NA',
    standard_error: 'NA',
    ci_lower: 'NA',
    ci_upper: 'NA',
    n_respondents: 'NA',
  })
  expect(rows[1]?.unavailable_reason).toContain('TUFNWGTP is undefined for 2020')

  const record = JSON.parse((await save(page, 'JSON')).text)
  const point2020 = record.response.points.find(
    (point: { year: number }) => point.year === 2020,
  )
  expect(point2020.estimate).toBeNull()
})

test('a comparison CSV holds both groups and the API difference', CORE, async ({ page }) => {
  await page.goto(
    analysisUrl('compare', {
      activity: { preset: 'sleep' },
      years: [2023],
      group_a: { sex: 'male' },
      group_b: { sex: 'female' },
      label_a: 'Men',
      label_b: 'Women, all ages',
    }),
  )
  await expect(page.getByTestId('difference')).toContainText('+33.3 min', { timeout: 15_000 })

  const rows = csvRecords((await save(page, 'CSV')).text)
  expect(rows.map((row) => [row.series, row.label])).toEqual([
    ['group_a', 'Men'],
    ['group_b', 'Women, all ages'],
    ['difference', 'Men minus Women, all ages'],
  ])
  expect(rows[0]?.estimate).toBe('100')
  expect(rows[2]).toMatchObject({ population: 'NA', n_respondents: 'NA' })

  const record = JSON.parse((await save(page, 'JSON')).text)
  expect(record.spec_source).toBe('request')
  expect(rows[1]?.estimate).toBe(String(record.response.group_b.estimate.value))
  expect(rows[2]?.estimate).toBe(String(record.response.difference.value))
  expect(rows[2]?.standard_error).toBe(String(record.response.difference.standard_error))
})

test(
  'downloads are keyboard-operable and announced without moving focus',
  CORE,
  async ({ page, browserName }) => {
    await page.goto(analysisUrl('estimate', { activity: { preset: 'sleep' }, years: [2023] }))
    await expect(page.locator('.stat-value')).toHaveText('1h 15m', { timeout: 15_000 })

    const button = page.getByRole('button', { name: 'Download CSV' })
    await button.focus()
    const downloading = page.waitForEvent('download')
    await page.keyboard.press('Enter')
    const download = await downloading
    expect(download.suggestedFilename()).toBe('atus-estimate-sleeping-2023.csv')
    await expect(button).toBeFocused()
    await expect(page.getByRole('status').filter({ hasText: 'Download started' })).toHaveText(
      'Download started: atus-estimate-sleeping-2023.csv',
    )
    await expectNoSeriousViolations(page)

    // The JSON download is the next tab stop.
    if (tabReachesAllControls(browserName)) {
      await tabTo(page, page.getByRole('button', { name: 'Download JSON' }), 1)
    }
  },
)
