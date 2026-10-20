/**
 * The `_headers` reader (Cloudflare Workers static assets semantics), the
 * site's actual rules in public/_headers, and `vite preview` applying them.
 */
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'

import {
  HeadersFileError,
  headersForPath,
  metaContentSecurityPolicy,
  parseHeadersFile,
} from './staticHeaders'

const SITE_HEADERS = readFileSync(join(process.cwd(), 'public', '_headers'), 'utf8')

describe('parseHeadersFile / headersForPath', () => {
  it('applies every matching rule; a header set twice is joined with a comma', () => {
    const rules = parseHeadersFile(
      ['# a comment', '/*', '  X-One: a', '', '/docs/*', '  X-One: b', '  X-Two: c'].join('\n'),
    )
    expect(Object.fromEntries(headersForPath(rules, '/docs/page'))).toEqual({
      'x-one': 'a, b',
      'x-two': 'c',
    })
    expect(Object.fromEntries(headersForPath(rules, '/other'))).toEqual({ 'x-one': 'a' })
  })

  it('matches a splat greedily and a placeholder within one path segment', () => {
    const rules = parseHeadersFile(
      [
        '/snap/current.json',
        '  Cache-Control: no-cache',
        '/snap/:id/*',
        '  Cache-Control: immutable',
      ].join('\n'),
    )
    expect(headersForPath(rules, '/snap/current.json').get('cache-control')).toBe('no-cache')
    expect(headersForPath(rules, '/snap/abc-123/manifest.json').get('cache-control')).toBe(
      'immutable',
    )
    expect(headersForPath(rules, '/snap/abc/post/k.json').get('cache-control')).toBe(
      'immutable',
    )
    expect(headersForPath(rules, '/snap/').get('cache-control')).toBeUndefined()
    expect(headersForPath(rules, '/snapshot/x/y').get('cache-control')).toBeUndefined()
  })

  it('treats pattern characters literally except * and :name', () => {
    const rules = parseHeadersFile(['/index.html', '  X: 1'].join('\n'))
    expect(headersForPath(rules, '/index.html').get('x')).toBe('1')
    expect(headersForPath(rules, '/indexAhtml').get('x')).toBeUndefined()
    expect(headersForPath(rules, '/index.html/more').get('x')).toBeUndefined()
  })

  it('detaches a header with "! Name"', () => {
    const rules = parseHeadersFile(
      ['/*', '  Content-Security-Policy: x', '/raw/*', '  ! Content-Security-Policy'].join(
        '\n',
      ),
    )
    expect(headersForPath(rules, '/raw/file').has('content-security-policy')).toBe(false)
    expect(headersForPath(rules, '/page').get('content-security-policy')).toBe('x')
  })

  it.each([
    ['a header before any rule', '  X: 1', /before any rule/],
    ['an absolute URL rule', 'https://example.org/*\n  X: 1', /URL path pattern/],
    ['a line without a colon', '/*\n  not a header', /Name: value/],
    ['an empty value', '/*\n  X:', /Name: value/],
    ['two splats', '/a/*/b/*\n  X: 1', /more than one splat/],
    ['a placeholder used twice', '/:a/:a\n  X: 1', /placeholder is used twice/],
    ['an over-long line', `/*\n  X: ${'v'.repeat(2_000)}`, /longer than 2000/],
    [
      'more than 100 rules',
      Array.from({ length: 101 }, (_, i) => `/r${i}\n  X: 1`).join('\n'),
      /more than 100 rules/,
    ],
  ])('rejects %s', (_label, text, message) => {
    expect(() => parseHeadersFile(text)).toThrow(HeadersFileError)
    expect(() => parseHeadersFile(text)).toThrow(message)
  })
})

describe('metaContentSecurityPolicy', () => {
  it('drops the directives a <meta> CSP cannot carry and keeps the rest in order', () => {
    expect(
      metaContentSecurityPolicy(
        "default-src 'none'; frame-ancestors 'none'; script-src 'self'; report-uri /r; sandbox;",
      ),
    ).toBe("default-src 'none'; script-src 'self'")
  })
})

describe('public/_headers (the deployed rules)', () => {
  const rules = parseHeadersFile(SITE_HEADERS)
  const policy = headersForPath(rules, '/').get('content-security-policy') ?? ''

  it('gives every page a strict CSP and the security headers', () => {
    for (const path of ['/', '/explore', '/about', '/analysis/trend', '/no-such-page']) {
      const headers = headersForPath(rules, path)
      expect(headers.get('content-security-policy'), path).toBe(policy)
      expect(headers.get('strict-transport-security'), path).toBe('max-age=31536000')
      expect(headers.get('x-content-type-options'), path).toBe('nosniff')
      expect(headers.get('referrer-policy'), path).toBe('no-referrer')
      expect(headers.get('cross-origin-opener-policy'), path).toBe('same-origin')
      expect(headers.get('permissions-policy'), path).toMatch(/camera=\(\).*geolocation=\(\)/)
    }
    expect(policy).toBe(
      "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; " +
        "font-src 'self'; connect-src 'self'; manifest-src 'self'; base-uri 'none'; " +
        "form-action 'none'; frame-ancestors 'none'",
    )
  })

  it('leaves clipboard writes to the page itself (the Share button copies its link)', () => {
    const permissions = headersForPath(rules, '/').get('permissions-policy') ?? ''
    expect(permissions).not.toMatch(/clipboard-write=\(\)/)
  })

  it('caches hashed bundles and snapshot directories forever, never the entry points', () => {
    const cache = (path: string) => headersForPath(rules, path).get('cache-control')
    expect(cache('/assets/index-DBowXUA-.js')).toBe('public, max-age=31536000, immutable')
    expect(cache('/snap/0123456789ab-0123456789abcdef/manifest.json')).toBe(
      'public, max-age=31536000, immutable',
    )
    expect(cache('/snap/0123456789ab-0123456789abcdef/post/ab.json')).toBe(
      'public, max-age=31536000, immutable',
    )
    for (const path of ['/', '/index.html', '/explore', '/about', '/analysis/estimate']) {
      expect(cache(path), path).toBe('no-cache')
    }
    // Exactly no-cache: no immutable rule also matches the pointer.
    expect(cache('/snap/current.json')).toBe('no-cache')
    // Unhashed files keep the host's default (revalidate every time).
    expect(cache('/boot.css')).toBeUndefined()
  })
})

describe('vite preview', () => {
  const scratch: string[] = []
  afterEach(() => {
    for (const dir of scratch.splice(0)) rmSync(dir, { recursive: true, force: true })
  })

  type Middleware = (
    request: { url?: string },
    response: { setHeader(name: string, value: string): void },
    next: () => void,
  ) => void

  async function previewMiddleware(): Promise<Middleware> {
    const root = mkdtempSync(join(tmpdir(), 'preview-headers-'))
    scratch.push(root)
    mkdirSync(join(root, 'dist'))
    writeFileSync(join(root, 'dist', '_headers'), SITE_HEADERS)
    const { default: factory } = await import('../../vite.config')
    const config = (factory as unknown as (env: object) => { plugins: unknown[] })({
      command: 'serve',
      mode: 'production',
      isSsrBuild: false,
      isPreview: true,
    })
    const plugin = config.plugins
      .flat(Infinity)
      .find(
        (candidate) =>
          (candidate as { name?: string } | null)?.name === 'atus-preview-static-headers',
      ) as { configurePreviewServer(server: object): void }
    let middleware: Middleware | null = null
    plugin.configurePreviewServer({
      config: { root, build: { outDir: 'dist' } },
      middlewares: { use: (fn: Middleware) => (middleware = fn) },
    })
    if (!middleware) throw new Error('no middleware registered')
    return middleware
  }

  function serve(middleware: Middleware, url: string): Map<string, string> {
    const headers = new Map<string, string>()
    let nextCalled = false
    middleware(
      { url },
      { setHeader: (name, value) => headers.set(name, value) },
      () => (nextCalled = true),
    )
    expect(nextCalled).toBe(true)
    return headers
  }

  it('serves the built files under the built _headers rules', async () => {
    const middleware = await previewMiddleware()
    const page = serve(middleware, '/analysis/estimate?spec=abc')
    expect(page.get('content-security-policy')).toContain("script-src 'self'")
    expect(page.get('cache-control')).toBe('no-cache')
    expect(serve(middleware, '/assets/index-x.js').get('cache-control')).toContain('immutable')
  })

  it('adds none of them to /api, which the edge Worker answers', async () => {
    const middleware = await previewMiddleware()
    expect(serve(middleware, '/api/v1/meta').size).toBe(0)
    expect(serve(middleware, '/api').size).toBe(0)
    expect(serve(middleware, '/apiary').size).toBeGreaterThan(0)
  })
})
