/**
 * Shared E2E helpers. All expected numbers derive from the hand-computed
 * fixture dataset (tests/integration/analytics_dataset.py in the repo root):
 *
 *   2023 sleep minutes: E1=100 (w=1, male 30), E2=200 (w=1, female 40),
 *   E3=0 (w=2, female 70)  → all: 75.0 min; ages 25–54: 150.0 min (n=2);
 *   men vs women: 100.0 vs 66.67, difference +33.33 (SE 11.785…).
 *   2019 sleep: 240.0. 2020 under multiyear weights: unavailable.
 */
import { type Page, expect } from '@playwright/test'

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
