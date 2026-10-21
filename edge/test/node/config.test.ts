/**
 * wrangler.jsonc invariants (ADR-011 "Deploy" and "Must-not-do list"),
 * read with Wrangler's own parser so CI fails before a config that could
 * bill, publish extra hostnames or expose the Worker on static paths ships.
 */

import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { experimental_readRawConfig, unstable_readConfig } from 'wrangler'

const CONFIG_PATH = fileURLToPath(new URL('../../wrangler.jsonc', import.meta.url))

const { rawConfig: raw } = experimental_readRawConfig({ config: CONFIG_PATH })
const config = unstable_readConfig({ config: CONFIG_PATH })

describe('wrangler.jsonc', () => {
  it('has no cache block anywhere (Workers Cache bills otherwise-free requests)', () => {
    expect(raw).not.toHaveProperty('cache')
    expect(JSON.stringify(raw)).not.toMatch(/"cache"\s*:/)
  })

  it('defines no environments, routes or custom domains (one workers.dev Worker)', () => {
    expect(raw).not.toHaveProperty('env')
    expect(raw).not.toHaveProperty('routes')
    expect(raw).not.toHaveProperty('route')
    expect(config.routes ?? []).toEqual([])
  })

  it('serves on workers.dev with Preview URLs off', () => {
    expect(raw.workers_dev).toBe(true)
    expect(raw.preview_urls).toBe(false)
  })

  it('runs the Worker first only for /api and /api/*', () => {
    expect(raw.assets?.run_worker_first).toEqual(['/api', '/api/*'])
  })

  it('serves the SPA build with single-page-application fallback', () => {
    expect(raw.assets?.not_found_handling).toBe('single-page-application')
    expect(raw.assets?.directory).toBe('../frontend/dist')
    expect(raw.assets?.binding).toBe('ASSETS')
  })

  it('uses the expected name, entry point and a compatibility date >= 2025-04-01', () => {
    expect(raw.name).toBe('atus')
    expect(raw.main).toBe('src/index.ts')
    expect(raw.compatibility_date! >= '2025-04-01').toBe(true)
  })

  it('declares the release vars with safe defaults and no secrets', () => {
    expect(raw.vars).toEqual({
      API_VERSION: 'v1',
      ANALYTICS_VERSION: '',
      DATA_VERSION: '',
      RESPONSE_FORMAT: '1',
      SNAPSHOT_ID: '',
      EPOCH: '1',
      PREWAKE: 'on',
      NEON_PROJECT_ID: '',
    })
  })

  it('requires the runtime secrets, so a deploy without them is refused', () => {
    expect(raw.secrets?.required).toEqual([
      'EDGE_AUTH_SECRET',
      'RENDER_ORIGIN',
      'OPS_TOKEN',
      'IP_HMAC_KEY',
    ])
  })

  it('binds one KV namespace and a SQLite-backed governor Durable Object', () => {
    expect(raw.kv_namespaces?.map((ns: { binding: string }) => ns.binding)).toEqual([
      'RESULT_CACHE',
    ])
    expect(raw.durable_objects?.bindings).toEqual([
      { name: 'GOVERNOR', class_name: 'Governor' },
    ])
    // Workers Free offers only the SQLite backend: never `new_classes`.
    expect(raw.migrations).toEqual([{ tag: 'v1', new_sqlite_classes: ['Governor'] }])
  })

  it('enables Workers Logs and disables Wrangler telemetry', () => {
    expect(raw.observability?.enabled).toBe(true)
    expect(raw.observability?.logs?.enabled).toBe(true)
    expect(raw.send_metrics).toBe(false)
  })

  it('keeps Workers invocation logs off: raw client IPs live only in memory (brief §12)', () => {
    // Cloudflare's per-request invocation records can carry the request's
    // headers, CF-Connecting-IP included, for the logs' retention. The
    // Worker's own JSON line already has the route, status, outcome and
    // duration (ADR-014, "Workers invocation logs").
    expect(raw.observability?.logs?.invocation_logs).toBe(false)
  })

  it('contains nothing that looks like a secret value', () => {
    const text = JSON.stringify(raw)
    expect(text).not.toMatch(/[0-9a-f]{40,}/i)
    expect(text).not.toMatch(/onrender\.com/)
  })
})
