/**
 * End-to-end tests against the REAL Phase 3 API serving the hand-computed
 * fixture database (scripts/e2e_backend.py) — deterministic, no BLS download
 * needed. The frontend is the production build, served by `vite preview`
 * exactly as it is deployed: built without VITE_API_BASE_URL, so it calls
 * /api/v1 on its own origin (the preview server proxies /api to the backend,
 * as the edge Worker does in production), under the production
 * Content-Security-Policy (vite.config.ts).
 *
 * Ports default to 4173 (frontend) and 8137 (backend); E2E_FRONTEND_PORT and
 * E2E_BACKEND_PORT override them when several checkouts run E2E at once.
 */
import { defineConfig, devices } from '@playwright/test'

const FRONTEND_PORT = Number(process.env.E2E_FRONTEND_PORT ?? 4173)
const BACKEND_PORT = Number(process.env.E2E_BACKEND_PORT ?? 8137)
const FRONTEND_ORIGIN = `http://localhost:${FRONTEND_PORT}`
const BACKEND_ORIGIN = `http://127.0.0.1:${BACKEND_PORT}`

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
      // /health needs no database (and no ops token), and the backend only
      // starts serving once its fixture database is loaded.
      command: `${process.env.E2E_BACKEND_PYTHON ?? '../.venv/bin/python'} ../scripts/e2e_backend.py --port ${BACKEND_PORT} --origin ${FRONTEND_ORIGIN} --origin http://127.0.0.1:${FRONTEND_PORT}`,
      url: `${BACKEND_ORIGIN}/health`,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: `npm run build && npm run preview -- --port ${FRONTEND_PORT} --strictPort`,
      url: FRONTEND_ORIGIN,
      reuseExistingServer: !process.env.CI,
      timeout: 180_000,
      // Same-origin production build: no VITE_API_BASE_URL. The preview
      // server forwards /api to the backend.
      env: { ATUS_DEV_API_ORIGIN: BACKEND_ORIGIN, VITE_API_BASE_URL: '' },
    },
  ],
})
