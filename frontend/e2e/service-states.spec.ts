/**
 * Phase 5 service states in a real browser, against the production build
 * served same-origin under the deployed `_headers` rules: cold start (a
 * delayed /meta), an incompatible API, a data release switch, an exhausted
 * quota, rate limits, the edge's daily request limit, the boot fallback when
 * the bundle cannot load, and the security headers and CSP compatibility of
 * the core journeys. Failures are injected with page.route; everything else
 * is the real API on the fixture database. New states get axe and keyboard
 * checks (brief §17).
 */
import { readFileSync } from 'node:fs'

import { type Page, expect, test } from '@playwright/test'

import {
  CORE,
  analysisUrl,
  analyze,
  collectPolicyErrors,
  expectNoSeriousViolations,
  setAge,
  tabReachesAllControls,
  tabTo,
} from './helpers'
import {
  headersForPath,
  metaContentSecurityPolicy,
  parseHeadersFile,
} from '../src/deploy/staticHeaders'

const SITE_HEADERS = parseHeadersFile(readFileSync('public/_headers', 'utf8'))

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

test(
  'a slow /meta shows the waking message politely, without moving focus',
  CORE,
  async ({ page, browserName }) => {
    await page.route('**/api/v1/meta', async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 7_000))
      await route.continue()
    })
    await page.goto('/explore')

    // <output> has the implicit role "status": a polite live region.
    const notice = page.getByRole('status').filter({ hasText: WAKING })
    await expect(notice).toBeVisible({ timeout: 6_000 })
    expect(await notice.evaluate((element) => element.tagName)).toBe('OUTPUT')
    await expect(page.getByRole('progressbar')).toHaveCount(0)
    expect(await page.evaluate(() => document.activeElement === document.body)).toBe(true)
    await expectNoSeriousViolations(page)
    // The page stays keyboard-operable while it waits.
    if (tabReachesAllControls(browserName)) {
      await tabTo(page, page.getByRole('link', { name: 'Skip to content' }), 3)
    }

    await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible({
      timeout: 15_000,
    })
    await expect(page.getByText(WAKING)).toHaveCount(0)
  },
)

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

test('a data release switch under an open page offers a reload, politely', async ({ page }) => {
  // /meta reports one release; analyses then come from another (a switch
  // happened after the page loaded).
  await page.route('**/api/v1/meta', async (route) => {
    const response = await route.fetch()
    await route.fulfill({
      response,
      headers: { ...response.headers(), 'x-data-version': 'e2e-release-a' },
    })
  })
  await page.route('**/api/v1/analysis/**', async (route) => {
    const response = await route.fetch()
    await route.fulfill({
      response,
      headers: { ...response.headers(), 'x-data-version': 'e2e-release-b' },
    })
  })
  await page.goto(sleep2023)
  await expect(page.locator('.stat-value')).toHaveText('1h 15m', { timeout: 15_000 })

  const notice = page.getByRole('status').filter({ hasText: 'different data release' })
  await expect(notice).toBeVisible()
  expect(await page.evaluate(() => document.activeElement === document.body)).toBe(true)
  await expectNoSeriousViolations(page)

  const reload = notice.getByRole('button', { name: 'Reload page' })
  await tabTo(page, reload, 40)
  const reloaded = page.waitForEvent('load')
  await page.keyboard.press('Enter')
  await reloaded
})

test("the API's own data versions: no notice while they agree, a reload offer when they differ", async ({
  page,
}) => {
  // Nothing is injected on /meta: the page learns its release from the
  // API's own X-Data-Version (or the static snapshot's pointer, which the
  // snapshot builder takes from that header).
  let switched = false
  const served: string[] = []
  await page.route('**/api/v1/analysis/**', async (route) => {
    const response = await route.fetch()
    const headers = response.headers()
    served.push(headers['x-data-version'] ?? '')
    await route.fulfill({
      response,
      headers: switched
        ? { ...headers, 'x-data-version': `${headers['x-data-version']}-next` }
        : headers,
    })
  })
  const notice = page.getByRole('status').filter({ hasText: 'different data release' })

  await page.goto(sleep2023)
  await expect(page.locator('.stat-value')).toHaveText('1h 15m', { timeout: 15_000 })
  expect(served.length).toBeGreaterThan(0)
  expect(served.every((version) => /^\d{4}:(load|serving)-/.test(version))).toBe(true)
  await expect(notice).toHaveCount(0)

  switched = true
  await page.goto(analysisUrl('estimate', { activity: { preset: 'sleep' }, years: [2019] }))
  await expect(page.locator('.stat-value').first()).toBeVisible({ timeout: 15_000 })
  await expect(notice).toBeVisible()
})

test('an API that is mid-switch (data_version_mismatch) is a temporary, accessible state', async ({
  page,
}) => {
  const posts = countPosts(page)
  await page.route('**/api/v1/analysis/**', (route) =>
    route.fulfill({
      status: 503,
      contentType: 'application/json',
      headers: { 'Retry-After': '1' },
      body: JSON.stringify({
        error: { code: 'data_version_mismatch', message: 'Switching.', details: null },
      }),
    }),
  )
  await page.goto(sleep2023)
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('The service is switching to a new data release', {
    timeout: 15_000,
  })
  // Never retried automatically: the visitor decides when to try again.
  expect(posts()).toBe(1)
  await expectNoSeriousViolations(page)
  const retry = page.getByRole('button', { name: 'Try again' })
  await expect(retry).toBeEnabled({ timeout: 5_000 })
  await tabTo(page, retry)
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
  await tabTo(page, page.getByRole('link', { name: 'Go to the home page' }))
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
  await expectNoSeriousViolations(page)
  await expect(retry).toBeEnabled({ timeout: 6_000 })
  expect(posts()).toBe(1)
  await tabTo(page, retry)
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

test(
  'the core journeys run same-origin under the deployed headers with no policy errors',
  CORE,
  async ({ page, baseURL }) => {
    const policyErrors = await collectPolicyErrors(page)
    const apiOrigins = new Set<string>()
    const bundleCaching: string[] = []
    page.on('request', (request) => {
      const url = new URL(request.url())
      if (url.pathname.startsWith('/api/')) apiOrigins.add(url.origin)
    })
    page.on('response', (response) => {
      if (/\/assets\/[^/]+\.js$/.test(new URL(response.url()).pathname)) {
        bundleCaching.push(response.headers()['cache-control'] ?? '')
      }
    })

    // Every page, including the routes the SPA fallback serves, carries the
    // _headers rules, and the build carries the <meta> CSP backstop.
    for (const path of ['/', '/explore', '/about']) {
      const response = await page.goto(path)
      const headers = response?.headers() ?? {}
      for (const [name, value] of headersForPath(SITE_HEADERS, path)) {
        expect(headers[name], `${path}: ${name}`).toBe(value)
      }
    }
    const policy = headersForPath(SITE_HEADERS, '/').get('content-security-policy') ?? ''
    expect(policy).toContain("script-src 'self'")
    await expect(page.locator('meta[http-equiv="Content-Security-Policy"]')).toHaveAttribute(
      'content',
      metaContentSecurityPolicy(policy),
    )
    expect(bundleCaching.length).toBeGreaterThan(0)
    for (const value of bundleCaching) expect(value).toContain('immutable')

    await page.goto('/')
    await expect(page.getByRole('link', { name: 'How much do Americans sleep?' })).toBeVisible()

    await page.goto('/explore')
    await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()
    await setAge(page, 25, 54)
    await page.getByLabel('Year', { exact: true }).selectOption('2023')
    await analyze(page)
    await expect(page.locator('.stat-value')).toHaveText('2h 30m', { timeout: 15_000 })
    await page.getByText('How was this calculated?').click()
    // A download is a Blob URL: allowed without any CSP exception.
    const downloading = page.waitForEvent('download')
    await page.getByRole('button', { name: 'Download CSV' }).click()
    expect((await downloading).suggestedFilename()).toBe('atus-estimate-sleeping-2023.csv')

    await page.goto('/explore')
    await page.getByRole('radio', { name: 'Trend over time' }).click()
    await page.getByLabel('From', { exact: true }).selectOption('2019')
    await analyze(page)
    await expect(page.getByTestId('unavailable-2020')).toBeVisible({ timeout: 15_000 })

    await page.goto('/about')
    await expect(page.getByRole('heading', { name: 'About ATUS Explorer' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Privacy and logs' })).toBeVisible()

    expect(policyErrors).toEqual([])
    expect([...apiOrigins]).toEqual([new URL(baseURL as string).origin])
  },
)
