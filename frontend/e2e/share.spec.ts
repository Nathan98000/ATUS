/**
 * Shareable analyses (spec §84): the copied URL must actually reconstruct
 * the analysis in a fresh browser context — same configuration, same result.
 */
import { expect, test } from './fixtures'

import { CORE, analyze, collectPolicyErrors, setAge } from './helpers'

test(
  'a shared analysis URL reproduces the analysis in a fresh browser context',
  CORE,
  async ({ page, browser, browserName }) => {
    // The site's Permissions-Policy denies clipboard reads (clipboard-read=()),
    // so the test records what the Share button writes instead of reading the
    // clipboard back; the real write still happens.
    if (browserName === 'chromium') await page.context().grantPermissions(['clipboard-write'])
    await page.addInitScript(() => {
      const clipboard = navigator.clipboard
      const write = clipboard.writeText.bind(clipboard)
      const copied: string[] = []
      Object.assign(window, { copiedForTest: copied })
      clipboard.writeText = (text: string) => {
        copied.push(text)
        return write(text)
      }
    })

    await page.goto('/explore')
    await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()
    await setAge(page, 25, 54)
    await page.getByLabel('Year', { exact: true }).selectOption('2023')
    await analyze(page)
    await expect(page.locator('.stat-value')).toHaveText('2h 30m', { timeout: 15_000 })

    await page.getByRole('button', { name: 'Share analysis' }).click()
    await expect(page.getByText('Link copied.')).toBeVisible()
    const sharedUrl = await page.evaluate(
      () => (window as unknown as { copiedForTest: string[] }).copiedForTest.at(-1) ?? '',
    )
    expect(sharedUrl).toBe(page.url())
    expect(sharedUrl).toContain('/analysis/estimate?spec=')

    // A completely fresh context: new storage, new cache, nothing carried over.
    const freshContext = await browser.newContext()
    const freshPolicyErrors = await collectPolicyErrors(freshContext)
    const freshPage = await freshContext.newPage()
    await freshPage.goto(sharedUrl)

    // Same result…
    await expect(freshPage.locator('.stat-value')).toHaveText('2h 30m', { timeout: 15_000 })
    await expect(freshPage.getByText(/Likely range/)).toContainText('2h 14m to 2h 46m')
    // …and the same reconstructable configuration.
    await expect(freshPage).toHaveTitle(/Sleeping — Ages 25–54 — 2023/)
    await freshPage.getByRole('link', { name: 'Adjust analysis' }).click()
    await expect(freshPage.getByLabel('Minimum age')).toHaveValue('25')
    await expect(freshPage.getByLabel('Maximum age')).toHaveValue('54')
    await expect(freshPage.getByLabel('Year', { exact: true })).toHaveValue('2023')
    expect(freshPolicyErrors).toEqual([])
    await freshContext.close()
  },
)

test('refreshing a result page reproduces the same analysis', CORE, async ({ page }) => {
  await page.goto('/explore')
  await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()
  await setAge(page, 25, 54)
  await page.getByLabel('Year', { exact: true }).selectOption('2023')
  await analyze(page)
  await expect(page.locator('.stat-value')).toHaveText('2h 30m', { timeout: 15_000 })

  await page.reload()
  await expect(page.locator('.stat-value')).toHaveText('2h 30m', { timeout: 15_000 })
  await expect(page.getByText(/Likely range/)).toContainText('2h 14m to 2h 46m')
})

test(
  'an invalid share link gets a helpful page, not a blank screen',
  CORE,
  async ({ page }) => {
    await page.goto('/analysis/estimate?spec=this-is-not-a-spec')
    await expect(page.getByRole('alert')).toContainText('This analysis link cannot be opened')
    await page.getByRole('link', { name: /Build an analysis/ }).click()
    await expect(page.getByRole('heading', { name: 'Build an analysis' })).toBeVisible()
  },
)

test('a link nested thousands of levels deep is an invalid link, not an app error (review round 1)', async ({
  page,
}) => {
  // ~8 KB, within URL limits; it used to overflow the stack in the
  // canonical serialization and show the application's error boundary.
  let posts = 0
  page.on('request', (request) => {
    if (request.method() === 'POST' && request.url().includes('/api/v1/analysis/')) posts += 1
  })
  const arrays = `${'['.repeat(3_000)}${']'.repeat(3_000)}`
  const json = `{"activity":{"preset":"sleep"},"years":[2023],"population":{"x":${arrays}}}`
  await page.goto(`/analysis/estimate?spec=${Buffer.from(json).toString('base64url')}`)
  await expect(
    page.getByRole('heading', { name: 'This analysis link cannot be opened' }),
  ).toBeVisible({ timeout: 15_000 })
  await expect(page.getByText(/nested too deeply/)).toBeVisible()
  await expect(page.getByText(/Something went wrong in the application/)).toHaveCount(0)
  expect(posts).toBe(0)
})
