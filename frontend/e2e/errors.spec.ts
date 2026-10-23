/**
 * Error UX against the real API (spec §87): analytical refusals show the
 * API's own explanation; infrastructure failures are distinguished from
 * invalid requests.
 */
import { expect, test, withoutSnapshot } from './fixtures'

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
  // 2020 alone runs under its own weights (review round 5); combined with a
  // year they do not cover, the API still refuses it.
  await page.goto(specUrl('estimate', { activity: { preset: 'sleep' }, years: [2020, 2023] }))
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('not statistically supported', { timeout: 15_000 })
  await expect(alert).toContainText('data collection was suspended')
  await expect(alert).toContainText('TU20FWGT')
  await expect(page.getByRole('link', { name: 'Adjust analysis' }).first()).toBeVisible()
})

test("a schema violation shows the API's field-level detail (review round 1)", async ({
  page,
}) => {
  // The real API's 422 envelope: details.errors[] = {location, message, type}.
  await page.goto(
    specUrl('estimate', { activity: { preset: 'sleep' }, measure: 'bogus', years: [2023] }),
  )
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('This analysis request is not valid', { timeout: 15_000 })
  await expect(alert.getByRole('listitem')).toContainText(/^measure: Input should be /)
  await expect(alert).toContainText('HTTP 422 · validation_error')
})

test('an analysis started offline says so, and runs when the connection returns (review round 1)', async ({
  page,
  context,
}) => {
  // In stack mode the starter's answer is in the static snapshot the home
  // page has already read, so it would show offline; this is about an
  // analysis that needs the service.
  await withoutSnapshot(page)
  await page.goto('/')
  const starter = page.getByRole('link', { name: /How much do Americans sleep\?/ })
  await expect(starter).toBeVisible({ timeout: 15_000 })
  await context.setOffline(true)
  await starter.click()
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('Check your internet connection', { timeout: 15_000 })
  await expect(page.getByText('Working out the number…')).toHaveCount(0)

  await context.setOffline(false)
  await expect(page.locator('.stat-value')).toBeVisible({ timeout: 15_000 })
  await expect(alert).toHaveCount(0)
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
  // Phase 5: an unreachable host is retried for a 150 s cold-start budget
  // (a sleeping free host looks exactly like this) before the failure shows.
  // The page clock lets the test jump over that budget.
  await page.clock.install()
  let blocked = true
  await page.route('**/api/v1/analysis/**', (route) => {
    if (blocked) return route.abort('connectionrefused')
    return route.fallback()
  })
  await page.goto(specUrl('estimate', { activity: { preset: 'sleep' }, years: [2023] }))
  // While it retries, the visitor is told the service is waking up.
  await expect(page.getByText(/Waking up the analysis service/)).toBeVisible({
    timeout: 15_000,
  })

  await page.clock.fastForward('02:40')
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('could not be reached', { timeout: 15_000 })

  blocked = false
  await page.getByRole('button', { name: 'Try again' }).click()
  await expect(page.locator('.stat-value')).toHaveText('1h 15m', { timeout: 15_000 }) // 75.0 min, all respondents 2023
})

test('a briefly unreachable backend is waited out without the visitor doing anything', async ({
  page,
}) => {
  let blocked = true
  await page.route('**/api/v1/analysis/**', (route) => {
    if (blocked) return route.abort('connectionrefused')
    return route.fallback()
  })
  await page.goto(specUrl('estimate', { activity: { preset: 'sleep' }, years: [2023] }))
  await expect(page.getByText(/Waking up the analysis service/)).toBeVisible({
    timeout: 15_000,
  })
  blocked = false
  await expect(page.locator('.stat-value')).toHaveText('1h 15m', { timeout: 20_000 })
  await expect(page.getByRole('alert')).toHaveCount(0)
})
