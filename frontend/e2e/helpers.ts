/**
 * Shared E2E helpers. All expected numbers derive from the hand-computed
 * fixture dataset (tests/integration/analytics_dataset.py in the repo root):
 *
 *   2023 sleep minutes: E1=100 (w=1, male 30), E2=200 (w=1, female 40),
 *   E3=0 (w=2, female 70)  → all: 75.0 min; ages 25–54: 150.0 min (n=2);
 *   men vs women: 100.0 vs 66.67, difference +33.33 (SE 11.785…).
 *   2019 sleep: 240.0. 2020 under multiyear weights: unavailable.
 */
import AxeBuilder from '@axe-core/playwright'
import { type Page, expect } from '@playwright/test'

/** Axe check: no serious or critical violations on the current page. */
export async function expectNoSeriousViolations(page: Page) {
  const results = await new AxeBuilder({ page }).analyze()
  const serious = results.violations.filter((violation) =>
    ['serious', 'critical'].includes(violation.impact ?? ''),
  )
  expect(
    serious.map((violation) => `${violation.id}: ${violation.nodes.length} nodes`),
  ).toEqual([])
}

/** Selects the sleep activity through search, as a user would. */
export async function pickSleepBySearch(page: Page) {
  await page.getByRole('button', { name: 'Change activity' }).click()
  await page.getByLabel('Search activities').fill('sleep')
  await page
    .getByRole('option', { name: /Sleeping/ })
    .first()
    .click()
  await expect(
    page.getByText('includes all 3 specific activities', { exact: false }),
  ).toBeVisible()
}

export async function setAge(page: Page, min: number, max: number) {
  await page.getByLabel('Minimum age').fill(String(min))
  await page.getByLabel('Maximum age').fill(String(max))
}

export async function analyze(page: Page) {
  await page.getByRole('button', { name: 'Analyze' }).click()
}

/** A share URL for an arbitrary spec (the app's encoding: base64url of sorted-keys JSON). */
export function analysisUrl(operation: string, spec: object): string {
  const sortKeys = (value: unknown): unknown => {
    if (Array.isArray(value)) return value.map(sortKeys)
    if (value !== null && typeof value === 'object') {
      return Object.fromEntries(
        Object.keys(value)
          .sort()
          .map((key) => [key, sortKeys((value as Record<string, unknown>)[key])]),
      )
    }
    return value
  }
  const base64 = Buffer.from(JSON.stringify(sortKeys(spec)), 'utf-8')
    .toString('base64')
    .replaceAll('+', '-')
    .replaceAll('/', '_')
    .replace(/=+$/, '')
  return `/analysis/${operation}?spec=${base64}`
}

/** Presses Tab until `target` has focus (fails after `maxTabs`): a keyboard reachability check. */
export async function tabTo(
  page: Page,
  target: import('@playwright/test').Locator,
  maxTabs = 25,
) {
  for (let presses = 0; presses < maxTabs; presses += 1) {
    if (await target.evaluate((element) => element === document.activeElement)) return
    await page.keyboard.press('Tab')
  }
  await expect(target).toBeFocused()
}
