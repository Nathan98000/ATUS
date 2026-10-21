/**
 * Browser smoke against an already running site: the deployed workers.dev
 * origin after `deploy.yml`, or the production compose stack behind the edge
 * Worker in CI (`ci.yml`, job e2e-stack).
 *
 *   PUBLIC_BASE_URL=https://atus.<sub>.workers.dev npx playwright test -c playwright.deployed.config.ts
 *
 * Starts no web server and talks to nothing but PUBLIC_BASE_URL. One
 * Chromium journey, no retries: the post-deploy smoke stays within its
 * request budget (ADR-011 "Deploy": at most 40 requests). The main suite
 * (playwright.config.ts) ignores these files: they end in `.deployed.ts`.
 */
import { defineConfig, devices } from '@playwright/test'

const BASE_URL = process.env.PUBLIC_BASE_URL
if (!BASE_URL) {
  throw new Error('set PUBLIC_BASE_URL to the site to smoke-test')
}

export default defineConfig({
  testDir: './e2e/deployed',
  testMatch: /.*\.deployed\.ts$/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: true,
  // A cold Render instance takes about a minute to wake (ADR-011).
  timeout: 240_000,
  reporter: 'list',
  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } },
    },
  ],
})
