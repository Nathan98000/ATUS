/// <reference types="vitest/config" />
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import react from '@vitejs/plugin-react'
import { type Plugin, defineConfig, loadEnv } from 'vite'

import { apiBaseConfigError, resolveApiBase } from './src/api/apiBase.ts'
import { ASSET_SALT, outputFileNames } from './src/deploy/assetNames.ts'
import {
  type HeaderRule,
  cspAllowsConnection,
  headersForPath,
  metaContentSecurityPolicy,
  parseHeadersFile,
} from './src/deploy/staticHeaders.ts'

function readHeaderRules(file: string): HeaderRule[] {
  return parseHeadersFile(readFileSync(file, 'utf8'))
}

/**
 * The SPA's Content-Security-Policy, from `<publicDir>/_headers` (the
 * deployed response-header rules): everything from its own origin, nothing
 * inline, no third parties (audit I06 measured 0 violations under it).
 */
export function siteContentSecurityPolicy(publicDir: string): string {
  const policy = headersForPath(readHeaderRules(resolve(publicDir, '_headers')), '/').get(
    'content-security-policy',
  )
  if (!policy) throw new Error(`${publicDir}/_headers sets no Content-Security-Policy for /`)
  return policy
}

/**
 * Why a build for this `VITE_API_BASE_URL` could not reach its API, or null.
 * The built page enforces the site's policy (the `<meta>` backstop, and the
 * `_headers` copy wherever the build is served), so an API on another origin
 * must be one the policy's `connect-src` allows — otherwise every request is
 * blocked in the browser, and the page retries a service it can never reach.
 */
export function crossOriginApiError(
  policy: string,
  configured: string | undefined,
): string | null {
  const base = resolveApiBase(configured)
  if (base === '' || cspAllowsConnection(policy, base)) return null
  return (
    `VITE_API_BASE_URL=${base} is another origin, but the site's Content-Security-Policy ` +
    `(public/_headers) does not allow connections to it, so the built page could not reach ` +
    `that API. Leave VITE_API_BASE_URL unset for the deployed same-origin setup (also when ` +
    `this URL is the site's own origin), use \`npm run dev\` for a development API on another ` +
    `origin, or add ${new URL(base).origin} to connect-src in public/_headers.`
  )
}

/**
 * Builds carry the same policy in a `<meta>` element as a backstop, in case
 * the host does not apply `_headers` to the SPA-fallback `index.html`
 * (ADR-011, an unverified detail). Build only: the dev server injects inline
 * scripts that the policy forbids. A build whose cross-origin API the policy
 * would block fails here.
 */
function metaCspPlugin(apiBase: string | undefined): Plugin {
  let policy = ''
  return {
    name: 'atus-meta-csp',
    apply: 'build',
    configResolved(config) {
      const sitePolicy = siteContentSecurityPolicy(config.publicDir)
      const apiError = crossOriginApiError(sitePolicy, apiBase)
      if (apiError !== null) throw new Error(apiError)
      policy = metaContentSecurityPolicy(sitePolicy)
    },
    transformIndexHtml: () => [
      {
        tag: 'meta',
        attrs: { 'http-equiv': 'Content-Security-Policy', content: policy },
        injectTo: 'head-prepend',
      },
    ],
  }
}

/**
 * The published OpenAPI schema: `frontend/openapi.json` (what
 * `scripts/export_openapi.py` writes) copied byte for byte to
 * `<outDir>/openapi.json`, so the static host serves it at `/openapi.json`
 * (ADR-011: the API's own `/openapi.json` is off in production). It is an
 * ordinary static asset outside `/api`, so the edge Worker never runs for it.
 * `npm run check:bundle` checks the copy.
 */
const OPENAPI_ASSET = 'openapi.json'

function publishOpenApiPlugin(): Plugin {
  let schemaFile = ''
  return {
    name: 'atus-publish-openapi',
    apply: 'build',
    configResolved(config) {
      schemaFile = resolve(config.root, OPENAPI_ASSET)
    },
    buildStart() {
      this.addWatchFile(schemaFile)
    },
    generateBundle() {
      this.emitFile({
        type: 'asset',
        fileName: OPENAPI_ASSET,
        source: readFileSync(schemaFile),
      })
    },
  }
}

/**
 * `vite preview` serves the build with the headers of the built `_headers`
 * file, as the static host does — so every E2E run checks the deployed CSP,
 * HSTS, Permissions-Policy and cache rules. `/api` is the edge Worker's (and
 * here the proxy's) and gets none of them.
 */
function previewStaticHeadersPlugin(): Plugin {
  return {
    name: 'atus-preview-static-headers',
    configurePreviewServer(server) {
      const outDir = resolve(server.config.root, server.config.build.outDir)
      const rules = readHeaderRules(resolve(outDir, '_headers'))
      server.middlewares.use((request, response, next) => {
        const pathname = new URL(request.url ?? '/', 'http://preview.invalid').pathname
        if (pathname !== '/api' && !pathname.startsWith('/api/')) {
          for (const [name, value] of headersForPath(rules, pathname)) {
            response.setHeader(name, value)
          }
        }
        next()
      })
    },
  }
}

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
    plugins: [
      react(),
      metaCspPlugin(env.VITE_API_BASE_URL),
      publishOpenApiPlugin(),
      previewStaticHeadersPlugin(),
    ],
    // Bundle names carry ASSET_SALT when set (the recovery after a Cloudflare
    // compromise, src/deploy/assetNames.ts); otherwise Vite's defaults.
    build: { rollupOptions: { output: outputFileNames(ASSET_SALT) } },
    server: { proxy: apiProxy },
    preview: { proxy: apiProxy },
    test: {
      environment: 'jsdom',
      setupFiles: ['src/test/setup.ts'],
      include: ['src/**/*.test.{ts,tsx}'],
      restoreMocks: true,
    },
  }
})
