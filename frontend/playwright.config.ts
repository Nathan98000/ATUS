/**
 * End-to-end tests against the REAL Phase 3 API serving the hand-computed
 * fixture database (scripts/e2e_backend.py) — deterministic, no BLS download
 * needed. The frontend is the production build, served by `vite preview`.
 */
import { defineConfig, devices } from '@playwright/test'

const FRONTEND_ORIGIN = 'http://localhost:4173'
const BACKEND_ORIGIN = 'http://127.0.0.1:8137'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'list' : [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: FRONTEND_ORIGIN,
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'desktop',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } },
      testIgnore: /mobile\.spec\.ts/,
    },
    {
      name: 'mobile',
      use: { ...devices['Pixel 7'] },
      testMatch: /mobile\.spec\.ts/,
    },
  ],
  webServer: [
    {
      // Locally the repo venv runs the backend; CI sets E2E_BACKEND_PYTHON=python.
      command: `${process.env.E2E_BACKEND_PYTHON ?? '../.venv/bin/python'} ../scripts/e2e_backend.py --port 8137 --origin ${FRONTEND_ORIGIN} --origin http://127.0.0.1:4173`,
      url: `${BACKEND_ORIGIN}/ready`,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: 'npm run build && npm run preview -- --port 4173 --strictPort',
      url: FRONTEND_ORIGIN,
      reuseExistingServer: !process.env.CI,
      timeout: 180_000,
      env: { VITE_API_BASE_URL: BACKEND_ORIGIN },
    },
  ],
})
