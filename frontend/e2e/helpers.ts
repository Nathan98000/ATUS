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
import { type Download, type Page, expect } from '@playwright/test'

import { browserPlatform, tabReachesAllControls as tabReachesAll } from './browserServer'

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

/**
 * The text of a downloaded file, byte-order mark included. The bytes are
 * streamed from wherever the browser saved them: `download.path()` throws
 * when the browser runs remotely (scripts/e2e_in_container.sh), the stream
 * works in both places.
 */
export async function downloadedText(download: Download) {
  const chunks: Buffer[] = []
  for await (const chunk of await download.createReadStream()) chunks.push(chunk as Buffer)
  return new TextDecoder('utf-8', { ignoreBOM: true }).decode(Buffer.concat(chunks))
}

/** CSV rows as header → value records (RFC 4180 quoting; the file starts with a BOM). */
export function csvRecords(text: string): Record<string, string>[] {
  expect(text.startsWith('﻿')).toBe(true)
  expect(text.endsWith('\r\n')).toBe(true)
  const rows: string[][] = []
  let row: string[] = []
  let field = ''
  let quoted = false
  const body = text.slice(1)
  for (let index = 0; index < body.length; index += 1) {
    const char = body[index] as string
    if (quoted) {
      if (char === '"' && body[index + 1] === '"') {
        field += '"'
        index += 1
      } else if (char === '"') quoted = false
      else field += char
    } else if (char === '"') quoted = true
    else if (char === ',') {
      row.push(field)
      field = ''
    } else if (char === '\r' && body[index + 1] === '\n') {
      row.push(field)
      rows.push(row)
      row = []
      field = ''
      index += 1
    } else field += char
  }
  const [header = [], ...data] = rows
  return data.map((cells) => {
    expect(cells).toHaveLength(header.length)
    return Object.fromEntries(header.map((name, index) => [name, cells[index] ?? '']))
  })
}

/**
 * Collects Content-Security-Policy violations (console messages and
 * `securitypolicyviolation` events) and Permissions-Policy header errors.
 * Call before the first navigation.
 */
export async function collectPolicyErrors(page: Page): Promise<string[]> {
  const errors: string[] = []
  page.on('console', (message) => {
    const text = message.text()
    if (/Content[- ]Security[- ]Policy|Permissions[- ]Policy|Feature[- ]Policy/i.test(text)) {
      errors.push(text)
    }
  })
  await page.addInitScript(() => {
    document.addEventListener('securitypolicyviolation', (event) => {
      console.error(`Content-Security-Policy violation: ${event.violatedDirective}`)
    })
  })
  return errors
}

/** Tag of the core journeys, which also run in Firefox and WebKit (playwright.config.ts). */
export const CORE = { tag: '@core' }

/**
 * Whether Tab reaches every link and button in this run's `browserName`
 * (e2e/browserServer.ts): not in WebKit on macOS, which tabs only between
 * text fields there. The platform is the browsers' own — Linux when they run
 * in the container (E2E_BROWSER_SERVER), wherever the test runner is.
 */
export function tabReachesAllControls(browserName: string): boolean {
  return tabReachesAll(
    browserName,
    browserPlatform(process.env.E2E_BROWSER_SERVER, process.platform),
  )
}
