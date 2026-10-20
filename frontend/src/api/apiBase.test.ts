/**
 * Audit F24: the API base is same-origin by default, an explicit base must be
 * an absolute URL, and builds never read .env files.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import { apiBaseConfigError, isAbsoluteHttpUrl, resolveApiBase } from './apiBase'
import { apiBaseUrl } from './client'

describe('resolveApiBase', () => {
  it('defaults to the same origin, never to a development address', () => {
    expect(resolveApiBase(undefined)).toBe('')
    expect(resolveApiBase('')).toBe('')
    expect(resolveApiBase('   ')).toBe('')
  })

  it('keeps an absolute http(s) base without trailing slashes', () => {
    expect(resolveApiBase('https://api.example.org/')).toBe('https://api.example.org')
    expect(resolveApiBase(' http://127.0.0.1:8137// ')).toBe('http://127.0.0.1:8137')
    expect(resolveApiBase('https://example.org/prefix/')).toBe('https://example.org/prefix')
  })

  it('treats relative or non-http values as same-origin', () => {
    expect(resolveApiBase('/')).toBe('')
    expect(resolveApiBase('api.example.org')).toBe('')
    expect(resolveApiBase('ftp://example.org')).toBe('')
  })

  it('is what the tests themselves run with: the production default', () => {
    expect(apiBaseUrl()).toBe('')
  })
})

describe('build-time validation', () => {
  it('accepts unset and absolute values', () => {
    expect(apiBaseConfigError(undefined)).toBeNull()
    expect(apiBaseConfigError('')).toBeNull()
    expect(apiBaseConfigError('https://api.example.org')).toBeNull()
    expect(isAbsoluteHttpUrl('http://localhost:8000')).toBe(true)
  })

  it('rejects anything else with an explanation', () => {
    expect(apiBaseConfigError('/')).toMatch(/absolute http\(s\) URL/)
    expect(apiBaseConfigError('api.example.org')).toMatch(/"api\.example\.org"/)
  })
})

describe('vite.config.ts', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
  })

  async function resolveConfig(command: 'build' | 'serve', mode: string) {
    const module = await import('../../vite.config')
    const factory = module.default as unknown as (env: {
      command: 'build' | 'serve'
      mode: string
      isSsrBuild: boolean
      isPreview: boolean
    }) => {
      envDir?: string | false
      plugins?: unknown[]
      server?: { proxy?: Record<string, unknown> }
      preview?: { proxy?: Record<string, unknown>; headers?: Record<string, string> }
    }
    return factory({ command, mode, isSsrBuild: false, isPreview: false })
  }

  it('previews (and so E2E-tests) the build under the deployed _headers rules', async () => {
    const module = await import('../../vite.config')
    const policy = module.siteContentSecurityPolicy('public')
    expect(policy).toContain("default-src 'none'")
    expect(policy).toContain("script-src 'self'")
    expect(policy).toContain("connect-src 'self'")
    expect(policy).not.toContain('unsafe-inline')

    const config = await resolveConfig('serve', 'production')
    const names = (config.plugins ?? [])
      .flat(Infinity)
      .map((plugin) => (plugin as { name?: string } | null)?.name)
    expect(names).toContain('atus-preview-static-headers')
    expect(names).toContain('atus-meta-csp')
    expect(config.preview?.headers).toBeUndefined()
    expect(config.preview?.proxy).toHaveProperty('/api')
  })

  it('production builds read no .env files (not even .env.local)', async () => {
    const config = await resolveConfig('build', 'production')
    expect(config.envDir).toBe(false)
  })

  it('development keeps .env files and proxies /api to a local API', async () => {
    const config = await resolveConfig('serve', 'development')
    expect(config.envDir).toBeUndefined()
    expect(config.server?.proxy).toHaveProperty('/api')
  })

  it('fails a production build whose VITE_API_BASE_URL is not absolute', async () => {
    vi.stubEnv('VITE_API_BASE_URL', 'api.example.org')
    await expect(resolveConfig('build', 'production')).rejects.toThrow(/VITE_API_BASE_URL/)
  })

  /** Runs the build's <meta> CSP plugin as `vite build` does once the config is resolved. */
  async function resolveMetaCsp() {
    const config = await resolveConfig('build', 'production')
    const plugin = (config.plugins ?? [])
      .flat(Infinity)
      .find(
        (candidate) => (candidate as { name?: string } | null)?.name === 'atus-meta-csp',
      ) as {
      configResolved: (resolved: { publicDir: string }) => void
    }
    plugin.configResolved({ publicDir: 'public' })
  }

  it('builds the same-origin bundle under the site CSP', async () => {
    await expect(resolveMetaCsp()).resolves.toBeUndefined()
  })

  it('fails a build whose cross-origin API the site CSP would block', async () => {
    // connect-src 'self' would block every request of the built page, which
    // would then retry a service it can never reach.
    vi.stubEnv('VITE_API_BASE_URL', 'https://api.example.org')
    await expect(resolveMetaCsp()).rejects.toThrow(
      /VITE_API_BASE_URL=https:\/\/api\.example\.org .*connect-src in public\/_headers/,
    )
  })

  it('explains the cross-origin conflict only for an absolute base the policy blocks', async () => {
    const { crossOriginApiError } = await import('../../vite.config')
    const site = "default-src 'none'; connect-src 'self'"
    expect(crossOriginApiError(site, undefined)).toBeNull()
    expect(crossOriginApiError(site, '')).toBeNull()
    expect(crossOriginApiError(site, 'http://localhost:8000/')).toMatch(
      /add http:\/\/localhost:8000 to connect-src/,
    )
    expect(
      crossOriginApiError(`${site} https://api.example.org`, 'https://api.example.org/v'),
    ).toBeNull()
  })
})
