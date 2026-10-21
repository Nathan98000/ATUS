/**
 * Every Phase 5 error code in a real browser (review round 1): the governor's
 * and the API's quota answers, the edge's own codes, and the deployed
 * cold-start path — a share link the static snapshot lacks, the prewake's
 * "waking", the edge's `service_waking` — with axe and keyboard checks of
 * each state (brief §14, §17). Failures are injected with page.route;
 * everything else is the real API on the fixture database. In stack mode a
 * last test lets the real API and Worker produce the code.
 */
import type { Page, Route } from '@playwright/test'

import { STACK_MODE, expect, test } from './fixtures'
import { analysisUrl, expectNoSeriousViolations, tabTo } from './helpers'

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

function envelope(
  route: Route,
  status: number,
  code: string,
  { retryAfter, details = null }: { retryAfter?: number; details?: unknown } = {},
) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    headers: retryAfter === undefined ? {} : { 'Retry-After': String(retryAfter) },
    body: JSON.stringify({ error: { code, message: `Injected ${code}.`, details } }),
  })
}

async function expectFocusUnmoved(page: Page) {
  expect(await page.evaluate(() => document.activeElement === document.body)).toBe(true)
}

test('capacity_paused (the governor’s monthly stop) is one honest, accessible page', async ({
  page,
}) => {
  const posts = countPosts(page)
  const resumesAt = new Date(Date.now() + 10 * 24 * 3600 * 1000).toISOString()
  await page.route('**/api/v1/analysis/**', (route) =>
    envelope(route, 503, 'capacity_paused', {
      retryAfter: 10 * 24 * 3600,
      details: { scope: 'monthly', budget: 'cu_month', resumes_at: resumesAt },
    }),
  )
  await page.goto(sleep2023)
  await expect(
    page.getByRole('heading', { name: 'New analyses are paused for now' }),
  ).toBeVisible({ timeout: 15_000 })
  const alert = page.getByRole('alert')
  await expect(alert).toHaveCount(1)
  await expect(alert).toContainText('free hosting')
  await expect(alert).toContainText(/\(around .+\)/)
  await expect(alert).toContainText('Starter examples')
  expect(posts()).toBe(1) // a paused quota is never retried automatically
  await expectFocusUnmoved(page)
  await expectNoSeriousViolations(page)
  await tabTo(page, page.getByRole('link', { name: 'Go to the home page' }))
  await tabTo(page, page.getByRole('button', { name: 'Try again' }))
})

test('daily_capacity_reached names the reset and offers no retry', async ({ page }) => {
  const posts = countPosts(page)
  await page.route('**/api/v1/analysis/**', (route) =>
    envelope(route, 503, 'daily_capacity_reached', {
      retryAfter: 3600,
      details: { scope: 'global', budget: 'global' },
    }),
  )
  await page.goto(sleep2023)
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('Today’s capacity for new analyses is used up', {
    timeout: 15_000,
  })
  await expect(alert).toContainText(/00:00 UTC \(.+ your time\)/)
  await expect(alert.getByRole('button', { name: 'Try again' })).toHaveCount(0)
  expect(posts()).toBe(1)
  await expectNoSeriousViolations(page)
})

test('analysis_timeout offers "Adjust analysis" and counts down its Retry-After', async ({
  page,
}) => {
  const posts = countPosts(page)
  await page.route('**/api/v1/analysis/**', (route) =>
    envelope(route, 503, 'analysis_timeout', { retryAfter: 2 }),
  )
  await page.goto(sleep2023)
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('This analysis took too long', { timeout: 15_000 })
  const retry = alert.getByRole('button', { name: 'Try again' })
  await expect(retry).toBeDisabled()
  await expect(alert.getByRole('link', { name: 'Adjust analysis' })).toBeVisible()
  await expectNoSeriousViolations(page)
  await expect(retry).toBeEnabled({ timeout: 5_000 })
  expect(posts()).toBe(1)
  await tabTo(page, alert.getByRole('link', { name: 'Adjust analysis' }))
})

test('overloaded is retried once automatically, then explained', async ({ page }) => {
  const posts = countPosts(page)
  await page.route('**/api/v1/analysis/**', (route) =>
    envelope(route, 503, 'overloaded', { retryAfter: 1, details: { scope: 'global' } }),
  )
  await page.goto(sleep2023)
  await expect(page.getByRole('alert')).toContainText('The analysis service is busy', {
    timeout: 15_000,
  })
  expect(posts()).toBe(2)
  await expectNoSeriousViolations(page)
  // Held back while its Retry-After runs; a disabled button takes no focus.
  const retry = page.getByRole('button', { name: 'Try again' })
  await expect(retry).toBeEnabled({ timeout: 5_000 })
  await tabTo(page, retry)
})

test('capacity_unavailable (the governor unreachable) is final and says what still works', async ({
  page,
}) => {
  const posts = countPosts(page)
  await page.route('**/api/v1/analysis/**', (route) =>
    envelope(route, 503, 'capacity_unavailable', { retryAfter: 1 }),
  )
  await page.goto(sleep2023)
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('New analyses are temporarily unavailable', {
    timeout: 15_000,
  })
  await expect(alert).toContainText('Starter examples')
  expect(posts()).toBe(1)
  await expectNoSeriousViolations(page)
})

test('the edge’s own refusals: payload_too_large, forbidden_origin, unsupported_media_type', async ({
  page,
}) => {
  const cases = [
    {
      status: 413,
      code: 'payload_too_large',
      heading: 'This analysis request is too large',
      adjust: true,
    },
    {
      status: 403,
      code: 'forbidden_origin',
      heading: 'This request was refused',
      adjust: false,
    },
    {
      status: 415,
      code: 'unsupported_media_type',
      heading: 'The analysis service did not recognise this request',
      adjust: false,
    },
  ]
  for (const { status, code, heading, adjust } of cases) {
    await page.unroute('**/api/v1/analysis/**')
    await page.route('**/api/v1/analysis/**', (route) => envelope(route, status, code))
    await page.goto(sleep2023)
    const alert = page.getByRole('alert')
    await expect(alert).toContainText(heading, { timeout: 15_000 })
    await expect(alert).toContainText(`HTTP ${status} · ${code}`)
    // None of them can be helped by repeating the request.
    await expect(alert.getByRole('button', { name: 'Try again' })).toHaveCount(0)
    await expect(alert.getByRole('link', { name: 'Adjust analysis' })).toHaveCount(
      adjust ? 1 : 0,
    )
    await expectNoSeriousViolations(page)
  }
})

test('service_waking past the cold-start budget is not promised to be temporary', async ({
  page,
}) => {
  // The edge's answer while the host wakes (or, indistinguishably, while it
  // is suspended): the page waits out the 150 s budget, then says so.
  await page.clock.install()
  await page.route('**/api/v1/analysis/**', (route) =>
    envelope(route, 503, 'service_waking', {
      retryAfter: 5,
      details: { retry_after_seconds: 5 },
    }),
  )
  await page.goto(sleep2023)
  const notice = page.getByRole('status').filter({ hasText: WAKING })
  await expect(notice).toBeVisible({ timeout: 15_000 })
  await expect(page.locator('.loading-panel')).toContainText(
    'Waiting for the analysis service to wake up…',
  )
  await expectFocusUnmoved(page)
  await expectNoSeriousViolations(page)

  await page.clock.fastForward('02:40')
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('The analysis service is still not answering', {
    timeout: 15_000,
  })
  await expect(alert).toContainText('more than 2 minutes')
  await expect(alert).toContainText('may be paused')
  await expect(alert).not.toContainText('in a moment')
  await expectNoSeriousViolations(page)
  const retry = alert.getByRole('button', { name: 'Try again' })
  await expect(retry).toBeEnabled({ timeout: 10_000 })
  await tabTo(page, retry)
})

test('upstream_unavailable connect_failed past the budget says the service could not be reached', async ({
  page,
}) => {
  await page.clock.install()
  await page.route('**/api/v1/analysis/**', (route) =>
    envelope(route, 502, 'upstream_unavailable', {
      retryAfter: 10,
      details: { reason: 'connect_failed' },
    }),
  )
  await page.goto(sleep2023)
  await expect(page.getByRole('status').filter({ hasText: WAKING })).toBeVisible({
    timeout: 15_000,
  })
  await page.clock.fastForward('02:40')
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('The analysis service could not be reached', {
    timeout: 15_000,
  })
  await expect(alert).not.toContainText('hosting provider answered')
  await expectNoSeriousViolations(page)
})

test('a share link the static snapshot lacks: prewake "waking", service_waking, then the result', async ({
  page,
}) => {
  // The deployed path (ADR-011): metadata comes from /snap/, so the page
  // never waits on /meta; an analysis the snapshot lacks prewakes the API
  // and waits on the edge's service_waking answers.
  const snapshotId = 'e2e0review01-0123456789abcdef'
  const meta = await page.request.get('/api/v1/meta')
  const dataVersion = meta.headers()['x-data-version'] ?? ''
  expect(dataVersion).not.toBe('')
  const metaBody = await meta.body()
  await page.route('**/snap/current.json', (route) =>
    route.fulfill({
      json: { snapshot_id: snapshotId, data_version: dataVersion, api_version: 'v1' },
    }),
  )
  await page.route(`**/snap/${snapshotId}/manifest.json`, (route) =>
    route.fulfill({
      json: {
        snapshot_id: snapshotId,
        data_version: dataVersion,
        get: { '/api/v1/meta': 'get/meta.json' },
        post: {},
      },
    }),
  )
  await page.route(`**/snap/${snapshotId}/get/meta.json`, (route) =>
    route.fulfill({ contentType: 'application/json', body: metaBody }),
  )
  let wakes = 0
  await page.route('**/api/v1/wake', (route) => {
    wakes += 1
    return route.fulfill({ json: { origin: 'waking', wake: 'sent' } })
  })
  let posts = 0
  await page.route('**/api/v1/analysis/**', (route) => {
    posts += 1
    return posts <= 2
      ? envelope(route, 503, 'service_waking', { retryAfter: 2 })
      : route.fallback()
  })

  await page.goto(sleep2023)
  const notice = page.getByRole('status').filter({ hasText: WAKING })
  await expect(notice).toBeVisible({ timeout: 10_000 })
  expect(await notice.evaluate((element) => element.tagName)).toBe('OUTPUT')
  await expect(page.locator('.loading-panel')).toContainText(
    'Waiting for the analysis service to wake up…',
  )
  await expect(page.getByRole('progressbar')).toHaveCount(0)
  // One live region announces the waking state, not three (review round 1).
  await expect(
    page
      .locator('output, [role="status"], [role="alert"], [aria-live]:not([aria-live="off"])')
      .filter({ hasText: /wak/i }),
  ).toHaveCount(1)
  await expectFocusUnmoved(page)
  await expectNoSeriousViolations(page)

  await expect(page.locator('.stat-value')).toHaveText('1h 15m', { timeout: 15_000 })
  await expect(notice).toHaveCount(0)
  await expect(page.getByRole('alert')).toHaveCount(0)
  expect(wakes).toBe(1)
  expect(posts).toBe(3)
})

test('stack mode: the real API’s per-visitor limit, through the real Worker', async ({
  page,
}) => {
  test.skip(!STACK_MODE, 'needs the production limits of the stack (E2E_BASE_URL)')
  // The API allows each visitor 6 new analyses a minute (ADR-012); the
  // Worker passes its 429 envelope through byte for byte. Seven analyses no
  // other test asks for (and no snapshot or KV entry holds) reach the API.
  const base = 20 + Math.floor(Math.random() * 90)
  for (let index = 0; index < 6; index += 1) {
    await page.goto(
      analysisUrl('estimate', {
        activity: { preset: 'sleep' },
        years: [2023],
        population: { age_min: base + index },
      }),
    )
    await expect(page.locator('.stat-value, [role="alert"]').first()).toBeVisible({
      timeout: 15_000,
    })
  }
  await page.goto(
    analysisUrl('estimate', {
      activity: { preset: 'sleep' },
      years: [2023],
      population: { age_min: base + 6 },
    }),
  )
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('Too many new analyses in a short time', {
    timeout: 15_000,
  })
  await expect(alert).toContainText('HTTP 429 · rate_limited')
  await expect(alert.getByRole('button', { name: 'Try again' })).toBeDisabled()
  await expectNoSeriousViolations(page)
})
