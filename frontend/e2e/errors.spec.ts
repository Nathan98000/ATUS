/**
 * Error UX against the real API (spec §87): analytical refusals show the
 * API's own explanation; infrastructure failures are distinguished from
 * invalid requests.
 */
import { expect, test } from '@playwright/test'

/** Builds a share URL for an arbitrary spec (same encoding as the app). */
function specUrl(operation: string, spec: object): string {
  const json = JSON.stringify(spec)
  const base64 = Buffer.from(json, 'utf-8')
    .toString('base64')
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replace(/=+$/, '')
  return `/analysis/${operation}?spec=${base64}`
}

test('the 2020 multiyear restriction shows the API explanation', async ({ page }) => {
  await page.goto(specUrl('estimate', { activity: { preset: 'sleep' }, years: [2020] }))
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('not statistically supported', { timeout: 15_000 })
  await expect(alert).toContainText('data collection was suspended')
  await expect(alert).toContainText('TU20FWGT')
  await expect(page.getByRole('link', { name: 'Adjust analysis' }).first()).toBeVisible()
})

test('an unknown activity code is refused with the lexicon explanation', async ({ page }) => {
  await page.goto(specUrl('estimate', { activity: { include: ['999999'] }, years: [2023] }))
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('Unknown activity', { timeout: 15_000 })
})

test('an analytically invalid population is a clear request problem', async ({ page }) => {
  await page.goto(
    specUrl('estimate', {
      activity: { preset: 'sleep' },
      years: [2023],
      population: { age_min: 90, age_max: 20 },
    }),
  )
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('not valid', { timeout: 15_000 })
  await expect(alert).toContainText('age_min 90 > age_max 20')
})

test('an empty population is explained, not rendered as zero', async ({ page }) => {
  // Fixture has no respondents this old in 2019.
  await page.goto(
    specUrl('estimate', {
      activity: { preset: 'sleep' },
      years: [2019],
      population: { age_min: 80 },
    }),
  )
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('Not enough data', { timeout: 15_000 })
  await expect(page.getByText('0h 0m')).toHaveCount(0)
})

test('a down backend is presented as unavailability with retry, then recovers', async ({
  page,
}) => {
  let blocked = true
  await page.route('**/api/v1/analysis/**', (route) => {
    if (blocked) return route.abort('connectionrefused')
    return route.fallback()
  })
  await page.goto(specUrl('estimate', { activity: { preset: 'sleep' }, years: [2023] }))
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('could not be reached', { timeout: 15_000 })

  blocked = false
  await page.getByRole('button', { name: 'Try again' }).click()
  await expect(page.locator('.stat-value')).toHaveText('1h 15m', { timeout: 15_000 }) // 75.0 min, all respondents 2023
})
