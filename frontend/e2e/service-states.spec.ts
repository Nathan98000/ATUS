/**
 * Phase 5 service states in a real browser, against the production build
 * served same-origin under the production CSP: cold start (a delayed /meta),
 * an incompatible API, an exhausted quota, the edge's daily request limit,
 * the boot fallback when the bundle cannot load, and CSP compatibility of the
 * core journeys. Failures are injected with page.route; everything else is
 * the real API on the fixture database. New states get axe and keyboard
 * checks (brief §17).
 */
import { type Page, expect, test } from '@playwright/test'

import { analysisUrl, analyze, expectNoSeriousViolations, setAge, tabTo } from './helpers'

const WAKING =
  'Waking up the analysis service — free hosting sleeps when idle; this can take up to a minute.'

const sleep2023 = analysisUrl('estimate', { activity: { preset: 'sleep' }, years: [2023] })

function countPosts(page: Page): () => number {
  let posts = 0
  page.on('request', (request) => {
    if (request.method() === 'POST' && request.url().includes('/api/v1/analysis/')) posts += 1
  })
  return () => posts
}

test('a slow /meta shows the waking message politely, without moving focus', async ({
  page,
}) => {
  await page.route('**/api/v1/meta', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 7_000))
    await route.continue()
  })
  await page.goto('/explore')

  const notice = page.locator('output', { hasText: WAKING }) // <output> = role "status" (polite)
  await expect(notice).toBeVisible({ timeout: 6_000 })
  await expect(page.getByRole('progressbar')).toHaveCount(0)
  expect(await page.evaluate(() => document.activeElement === document.body)).toBe(true)
  await expectNoSeriousViolations(page)

  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible({
    timeout: 15_000,
  })
  await expect(page.getByText(WAKING)).toHaveCount(0)
})

test('a share link waiting on /meta is "preparing", and sends nothing until /meta answers', async ({
  page,
}) => {
  const posts = countPosts(page)
  await page.route('**/api/v1/meta', async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 4_000))
    await route.continue()
  })
  await page.goto(sleep2023)
  await expect(page.locator('.loading-panel')).toContainText('Preparing the analysis…')
  await expect(page.getByText('Calculating estimate…')).toHaveCount(0)
  expect(posts()).toBe(0)
  await expect(page.locator('.stat-value')).toHaveText('1h 15m', { timeout: 15_000 })
})

test('an incompatible API version is one clear, accessible full-page state', async ({
  page,
}) => {
  const posts = countPosts(page)
  await page.route('**/api/v1/meta', async (route) => {
    const response = await route.fetch()
    await route.fulfill({ response, json: { ...(await response.json()), api_version: 'v2' } })
  })
  await page.goto(sleep2023)
  await expect(
    page.getByRole('heading', { name: 'This page and the analysis service are out of step' }),
  ).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('alert')).toContainText('version v2')
  expect(posts()).toBe(0)
  await expectNoSeriousViolations(page)
  await tabTo(page, page.getByRole('button', { name: 'Reload the page' }))
})

test('an exhausted database quota is one honest, accessible page', async ({ page }) => {
  await page.route('**/api/v1/analysis/**', (route) =>
    route.fulfill({
      status: 503,
      contentType: 'application/json',
      headers: { 'Retry-After': '86400' },
      body: JSON.stringify({
        error: { code: 'database_paused', message: 'Quota exceeded.', details: null },
      }),
    }),
  )
  await page.goto(sleep2023)
  await expect(
    page.getByRole('heading', { name: 'The analysis database is paused' }),
  ).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('alert')).toHaveCount(1)
  await expectNoSeriousViolations(page)

  const home = page.getByRole('link', { name: 'Go to the home page' })
  await tabTo(page, home)
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { name: /Explore how Americans/ })).toBeVisible()
})

test("the edge's daily request limit (a non-JSON 429) is named as such", async ({ page }) => {
  await page.route('**/api/v1/meta', (route) =>
    route.fulfill({
      status: 429,
      contentType: 'text/html',
      body: '<html><body>Error 1027</body></html>',
    }),
  )
  await page.goto('/explore')
  await expect(
    page.getByRole('heading', { name: 'ATUS Explorer has reached today’s request limit' }),
  ).toBeVisible({ timeout: 15_000 })
  await expect(page.getByText(/problem in the service/)).toHaveCount(0)
  await expectNoSeriousViolations(page)
})

test('a rate-limited analysis counts down its Retry-After before "Try again"', async ({
  page,
}) => {
  const posts = countPosts(page)
  await page.route('**/api/v1/analysis/**', (route) =>
    route.fulfill({
      status: 429,
      contentType: 'application/json',
      headers: { 'Retry-After': '3' },
      body: JSON.stringify({
        error: { code: 'rate_limited', message: 'Too many.', details: { scope: 'client' } },
      }),
    }),
  )
  await page.goto(sleep2023)
  const retry = page.getByRole('button', { name: 'Try again' })
  await expect(retry).toBeDisabled({ timeout: 15_000 })
  await expect(page.getByRole('alert')).toContainText('about 3 seconds')
  await expect(retry).toBeEnabled({ timeout: 6_000 })
  expect(posts()).toBe(1)
})

test('if the app bundle cannot load, the page explains instead of staying blank', async ({
  page,
}) => {
  await page.route('**/assets/*.js', (route) => route.abort())
  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'ATUS Explorer did not start' })).toBeVisible({
    timeout: 15_000,
  })
  await expect(page.getByRole('link', { name: 'Reload the page' })).toBeVisible()
})

test('the core journeys run same-origin under the production CSP with no violations', async ({
  page,
  baseURL,
}) => {
  const violations: string[] = []
  const apiOrigins = new Set<string>()
  page.on('console', (message) => {
    if (/Content[- ]Security[- ]Policy/i.test(message.text())) violations.push(message.text())
  })
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (url.pathname.startsWith('/api/')) apiOrigins.add(url.origin)
  })
  await page.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (event) => {
      console.error(`Content-Security-Policy violation: ${event.violatedDirective}`)
    })
  })

  const response = await page.goto('/')
  expect(response?.headers()['content-security-policy']).toContain("script-src 'self'")
  await expect(page.getByRole('link', { name: 'How much do Americans sleep?' })).toBeVisible()

  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()
  await setAge(page, 25, 54)
  await page.getByLabel('Year', { exact: true }).selectOption('2023')
  await analyze(page)
  await expect(page.locator('.stat-value')).toHaveText('2h 30m', { timeout: 15_000 })
  await page.getByText('How was this calculated?').click()

  await page.goto('/explore')
  await page.getByRole('radio', { name: 'Trend over time' }).click()
  await page.getByLabel('From', { exact: true }).selectOption('2019')
  await analyze(page)
  await expect(page.getByTestId('unavailable-2020')).toBeVisible({ timeout: 15_000 })

  await page.goto('/about')
  await expect(page.getByRole('heading', { name: 'About ATUS Explorer' })).toBeVisible()

  expect(violations).toEqual([])
  expect([...apiOrigins]).toEqual([new URL(baseURL as string).origin])
})
