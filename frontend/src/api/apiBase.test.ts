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
    }) => { envDir?: string | false; server?: { proxy?: Record<string, unknown> } }
    return factory({ command, mode, isSsrBuild: false, isPreview: false })
  }

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
})
