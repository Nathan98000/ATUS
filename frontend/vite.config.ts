/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

import { apiBaseConfigError } from './src/api/apiBase.ts'

/**
 * The SPA's Content-Security-Policy: everything from its own origin, nothing
 * inline, no third parties (audit I06 measured 0 violations under it).
 * `vite preview` — and therefore every E2E run — serves the production build
 * under it, so a change that needs a weaker policy fails a test. The dev
 * server does not: Vite injects inline scripts there.
 */
export const CONTENT_SECURITY_POLICY = [
  "default-src 'none'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self'",
  "font-src 'self'",
  "connect-src 'self'",
  "manifest-src 'self'",
  "base-uri 'none'",
  "form-action 'none'",
  "frame-ancestors 'none'",
].join('; ')

// https://vite.dev/config/
export default defineConfig(({ command, mode }) => {
  // Builds and tests read configuration from the process environment only.
  // A developer's frontend/.env.local must never leak into a deployable
  // bundle (audit F24) or make tests depend on one machine.
  const envDir = command === 'build' || mode === 'test' ? false : undefined
  const env = loadEnv(mode, envDir === false ? false : process.cwd(), ['VITE_', 'ATUS_DEV_'])

  // Production builds call the API on their own origin (relative /api/v1)
  // unless VITE_API_BASE_URL is an absolute URL; anything else fails the build
  // instead of silently producing a bundle that calls the wrong host.
  const baseError = apiBaseConfigError(env.VITE_API_BASE_URL)
  if (command === 'build' && baseError !== null) throw new Error(baseError)

  // `npm run dev` / `npm run preview` forward same-origin /api requests to a
  // local API, mirroring the production edge (no CORS needed).
  const apiProxy = {
    '/api': { target: env.ATUS_DEV_API_ORIGIN || 'http://localhost:8000', changeOrigin: false },
  }

  return {
    envDir,
    plugins: [react()],
    server: { proxy: apiProxy },
    preview: {
      proxy: apiProxy,
      headers: { 'Content-Security-Policy': CONTENT_SECURITY_POLICY },
    },
    test: {
      environment: 'jsdom',
      setupFiles: ['src/test/setup.ts'],
      include: ['src/**/*.test.{ts,tsx}'],
      restoreMocks: true,
    },
  }
})
