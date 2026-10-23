/**
 * scripts/check-bundle.mjs, the deploy-time guard on dist/: no baked-in
 * development API (F24), no inline code the CSP forbids, the <meta> CSP
 * backstop, the _headers rules, and the published OpenAPI schema. The last
 * test builds the real app and checks the result.
 */
import { spawnSync } from 'node:child_process'
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'

import {
  headersForPath,
  metaContentSecurityPolicy,
  parseHeadersFile,
} from './deploy/staticHeaders'

const FRONTEND = process.cwd()
const SITE_HEADERS = readFileSync(join(FRONTEND, 'public', '_headers'), 'utf8')
/** The committed schema the build publishes at /openapi.json (ADR-011). */
const OPENAPI = readFileSync(join(FRONTEND, 'openapi.json'))
const POLICY =
  headersForPath(parseHeadersFile(SITE_HEADERS), '/').get('content-security-policy') ?? ''
const META = `<meta http-equiv="Content-Security-Policy" content="${metaContentSecurityPolicy(POLICY).replaceAll("'", '&#39;')}">`

const scratch: string[] = []

function indexHtml({ meta = META, extra = '' } = {}): string {
  return [
    '<!doctype html><html><head>',
    meta,
    '<meta charset="UTF-8" />',
    '<link rel="stylesheet" href="/boot.css" />',
    '<script type="module" crossorigin src="/assets/index-abc123.js"></script>',
    extra,
    '</head><body><div id="root"></div></body></html>',
  ].join('\n')
}

function distWith({
  code = 'fetch("/api/v1/meta")',
  html = indexHtml(),
  headers = SITE_HEADERS as string | null,
  openapi = OPENAPI as Buffer | string | null,
} = {}): string {
  const dist = mkdtempSync(join(tmpdir(), 'check-bundle-'))
  scratch.push(dist)
  mkdirSync(join(dist, 'assets'))
  writeFileSync(join(dist, 'assets', 'index-abc123.js'), code)
  writeFileSync(join(dist, 'index.html'), html)
  if (headers !== null) writeFileSync(join(dist, '_headers'), headers)
  if (openapi !== null) writeFileSync(join(dist, 'openapi.json'), openapi)
  return dist
}

function check(dist: string) {
  return spawnSync(process.execPath, ['scripts/check-bundle.mjs', dist], {
    cwd: FRONTEND,
    encoding: 'utf8',
  })
}

function expectFailure(dist: string, message: string | RegExp) {
  const result = check(dist)
  expect(result.status, result.stderr).toBe(1)
  expect(result.stderr).toMatch(message)
}

afterEach(() => {
  for (const dir of scratch.splice(0)) rmSync(dir, { recursive: true, force: true })
})

describe('check-bundle.mjs', () => {
  it('passes a deployable dist', () => {
    const result = check(distWith())
    expect(result.stderr).toBe('')
    expect(result.status).toBe(0)
    expect(result.stdout).toContain('no inline code and the <meta> CSP')
  })

  it('fails on a development API address', () => {
    for (const address of [
      'http://localhost:8000',
      'http://127.0.0.1:8137',
      'http://[::1]:8000',
    ]) {
      const dist = distWith({ code: `fetch(${JSON.stringify(address)}+"/api/v1/meta")` })
      expectFailure(dist, 'development API address')
      expect(check(dist).stderr).toContain('Build without VITE_API_BASE_URL')
    }
  })

  it("passes react-router's portless URL base", () => {
    const dist = distWith({
      code: 'var a=`/api/v1`;var b=new URL(`http://localhost`);fetch(a+"/meta")',
    })
    expect(check(dist).status).toBe(0)
  })

  it.each([
    ['an inline script', '<script>alert(1)</script>', /inline script/],
    ['a script with both src and a body', '<script src="/a.js">x()</script>', /inline script/],
    ['a <style> element', '<style>p{}</style>', /<style> element/],
    ['a style attribute', '<p style="color:red">x</p>', /style attribute/],
    ['an inline event handler', '<img src="/a.svg" onerror="x()">', /inline event handler/],
    ['a javascript: URL', '<a href="javascript:void(0)">x</a>', /javascript: URL/],
  ])('fails on %s in the HTML', (_label, extra, message) => {
    expectFailure(distWith({ html: indexHtml({ extra }) }), message)
  })

  it('ignores markup inside HTML comments', () => {
    const html = indexHtml({ extra: '<!-- <script>not code</script> style="x" -->' })
    expect(check(distWith({ html })).status).toBe(0)
  })

  it('requires the <meta> CSP backstop, equal to _headers, before any script or stylesheet', () => {
    expectFailure(
      distWith({ html: indexHtml({ meta: '' }) }),
      /no <meta> Content-Security-Policy/,
    )
    expectFailure(
      distWith({
        html: indexHtml({
          meta: META.replace('script-src &#39;self&#39;', 'script-src &#39;self&#39; https:'),
        }),
      }),
      /differs from _headers/,
    )
    const late = indexHtml({ meta: '' }).replace('</head>', `${META}</head>`)
    expectFailure(distWith({ html: late }), /comes after a script or stylesheet/)
  })

  it('requires _headers', () => {
    const result = check(distWith({ headers: null }))
    expect(result.status).toBe(2)
    expect(result.stderr).toContain('_headers')
  })

  it('fails when _headers is malformed', () => {
    expectFailure(distWith({ headers: 'X-Orphan: 1\n' }), /_headers: _headers line 1/)
  })

  it('fails when a security header is missing or weakened', () => {
    expectFailure(
      distWith({ headers: SITE_HEADERS.replace(/^ {2}Strict-Transport-Security:.*\n/m, '') }),
      /lacks a valid strict-transport-security/,
    )
    expectFailure(
      distWith({ headers: SITE_HEADERS.replace('max-age=31536000\n', 'max-age=60\n') }),
      /lacks a valid strict-transport-security/,
    )
    expectFailure(
      distWith({ headers: SITE_HEADERS.replace('nosniff', 'sniff') }),
      /lacks a valid x-content-type-options/,
    )
    expectFailure(
      distWith({
        headers: SITE_HEADERS.replace("script-src 'self'", "script-src 'self' 'unsafe-inline'"),
      }),
      /script-src is not 'self'/,
    )
    expectFailure(
      distWith({
        headers: SITE_HEADERS.replace("img-src 'self'", "img-src 'self' data:"),
      }),
      /allows unsafe-\*, data: or a wildcard/,
    )
    expectFailure(
      distWith({ headers: SITE_HEADERS.replace("frame-ancestors 'none'", '') }),
      /frame-ancestors is not 'none'/,
    )
  })

  it('fails when the cache rules would pin an entry point or let bundles go stale', () => {
    // A broad snapshot rule would be joined onto the pointer's no-cache.
    expectFailure(
      distWith({ headers: SITE_HEADERS.replace('/snap/:snapshot/*', '/snap/*') }),
      /\/snap\/current\.json must be Cache-Control: no-cache/,
    )
    expectFailure(
      distWith({ headers: SITE_HEADERS.replace('/assets/*', '/static/*') }),
      /index-abc123\.js is not cached as immutable/,
    )
    expectFailure(
      distWith({
        headers: SITE_HEADERS.replace('/index.html\n  Cache-Control: no-cache\n', ''),
      }),
      /\/index\.html must be Cache-Control: no-cache/,
    )
  })

  it('requires the published OpenAPI schema, byte for byte frontend/openapi.json', () => {
    expectFailure(distWith({ openapi: null }), /openapi\.json is missing/)
    // A stale copy, and the same schema serialized differently (not what
    // scripts/export_openapi.py wrote): both differ.
    const schema = JSON.parse(OPENAPI.toString('utf8')) as { info: { version: string } }
    expectFailure(
      distWith({
        openapi: `${JSON.stringify({ ...schema, info: { ...schema.info, version: 'old' } }, null, 1)}\n`,
      }),
      /openapi\.json differs from frontend\/openapi\.json/,
    )
    expectFailure(
      distWith({ openapi: JSON.stringify(schema) }),
      /openapi\.json differs from frontend\/openapi\.json/,
    )
  })

  it('requires the schema to be revalidated, never cached as immutable', () => {
    const withoutRule = SITE_HEADERS.replace('/openapi.json\n  Cache-Control: no-cache\n', '')
    expect(withoutRule).not.toBe(SITE_HEADERS)
    expectFailure(
      distWith({ headers: withoutRule }),
      /\/openapi\.json must be Cache-Control: no-cache, is \(default\)/,
    )
  })

  it('fails when the build puts files under /api, which the edge Worker answers first', () => {
    const dist = distWith()
    mkdirSync(join(dist, 'api'))
    writeFileSync(join(dist, 'api', 'openapi.json'), OPENAPI)
    expectFailure(dist, /dist\/api\/ exists, but \/api belongs to the edge Worker/)
  })

  it('passes the real production build: no inline code, the <meta> CSP first, _headers shipped', () => {
    const outDir = mkdtempSync(join(tmpdir(), 'check-bundle-build-'))
    scratch.push(outDir)
    const env: NodeJS.ProcessEnv = { ...process.env }
    delete env.VITE_API_BASE_URL
    const build = spawnSync(
      process.execPath,
      [
        'node_modules/vite/bin/vite.js',
        'build',
        '--outDir',
        outDir,
        '--emptyOutDir',
        '--logLevel',
        'error',
      ],
      { cwd: FRONTEND, encoding: 'utf8', env },
    )
    expect(build.status, build.stderr).toBe(0)

    const result = check(outDir)
    expect(result.stderr).toBe('')
    expect(result.status).toBe(0)

    expect(readFileSync(join(outDir, '_headers'), 'utf8')).toBe(SITE_HEADERS)
    // The schema is published at the site root, outside /api, byte for byte.
    expect(readFileSync(join(outDir, 'openapi.json')).equals(OPENAPI)).toBe(true)
    expect(existsSync(join(outDir, 'api'))).toBe(false)
    const html = readFileSync(join(outDir, 'index.html'), 'utf8')
    const page = new DOMParser().parseFromString(html, 'text/html')
    const first = page.head.firstElementChild
    expect(first?.getAttribute('http-equiv')).toBe('Content-Security-Policy')
    expect(first?.getAttribute('content')).toBe(metaContentSecurityPolicy(POLICY))
    for (const script of Array.from(page.querySelectorAll('script'))) {
      expect(script.getAttribute('src')).toMatch(/^\/assets\/index-[\w-]+\.js$/)
      expect(script.textContent).toBe('')
    }
    expect(readdirSync(join(outDir, 'assets')).some((name) => name.endsWith('.js'))).toBe(true)
  }, 120_000)
})
