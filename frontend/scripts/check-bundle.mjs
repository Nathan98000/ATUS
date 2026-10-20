#!/usr/bin/env node
/**
 * The deploy-time guard on a built `dist/`. Fails (exit 1) when:
 *
 * 1. the bundle still points at a development API (audit F24): a loopback
 *    address with a port (`localhost:8000`, `127.0.0.1:8137`, `[::1]:8000`).
 *    A production build must call its own origin (relative /api/v1) unless
 *    VITE_API_BASE_URL deliberately names another one. react-router uses a
 *    bare `http://localhost` (no port) internally as a URL parsing base;
 *    that is not an address the app calls, so it is not matched;
 * 2. an HTML file needs anything the strict CSP forbids: an inline script,
 *    a `<style>` element or `style` attribute, an inline event handler, a
 *    `javascript:` URL;
 * 3. `index.html` lacks the `<meta>` CSP backstop, or it differs from the
 *    `_headers` policy, or it comes after a script or stylesheet;
 * 4. `dist/_headers` is missing or malformed, or does not give the site the
 *    security headers and cache rules ADR-011 relies on.
 *
 * Usage: node scripts/check-bundle.mjs [distDir]   (default: dist)
 * Needs Node >= 22.18 (it imports src/deploy/staticHeaders.ts directly).
 */
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

import {
  headersForPath,
  metaContentSecurityPolicy,
  parseHeadersFile,
} from '../src/deploy/staticHeaders.ts'

const DEV_API_ADDRESS = /(?:localhost|127\.0\.0\.1|\[::1\]):\d+/g

const dist = process.argv[2] ?? 'dist'
const findings = []

function read(path) {
  try {
    return readFileSync(path, 'utf8')
  } catch (error) {
    process.stderr.write(`check-bundle: cannot read ${path}: ${error.message}\n`)
    process.exit(2)
  }
}

function list(dir) {
  try {
    return readdirSync(dir)
  } catch (error) {
    process.stderr.write(`check-bundle: cannot read ${dir}: ${error.message}\n`)
    process.exit(2)
  }
}

// 1. Development API addresses in the JavaScript bundle.
const assetNames = list(join(dist, 'assets'))
const scripts = assetNames.filter((name) => name.endsWith('.js'))
for (const name of scripts) {
  const text = read(join(dist, 'assets', name))
  for (const match of text.matchAll(DEV_API_ADDRESS)) {
    findings.push(`development API address in ${name}: ${match[0]}`)
  }
}

// 2. HTML that needs an inline exception to the CSP.
const htmlFiles = list(dist).filter((name) => name.endsWith('.html'))
for (const name of htmlFiles) {
  const html = read(join(dist, name)).replace(/<!--[\s\S]*?-->/g, '')
  for (const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
    if (!/\bsrc\s*=/i.test(match[1]) || match[2].trim() !== '') {
      findings.push(`inline script in ${name}: ${match[0].slice(0, 80)}`)
    }
  }
  if (/<style\b/i.test(html)) findings.push(`<style> element in ${name}`)
  if (/<[^>]+\sstyle\s*=/i.test(html)) findings.push(`style attribute in ${name}`)
  if (/<[^>]+\son[a-z]+\s*=/i.test(html)) findings.push(`inline event handler in ${name}`)
  if (/javascript:/i.test(html)) findings.push(`javascript: URL in ${name}`)
}

// 4 (read first). The static host's header rules.
let rules = []
try {
  rules = parseHeadersFile(read(join(dist, '_headers')))
} catch (error) {
  findings.push(`_headers: ${error.message}`)
}
const headersFor = (path) => headersForPath(rules, path)
const policy = headersFor('/').get('content-security-policy') ?? ''

// 3. The <meta> CSP backstop in index.html.
const index = read(join(dist, 'index.html'))
const meta = /<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]*)"\s*\/?>/i.exec(
  index,
)
if (!meta) {
  findings.push('index.html has no <meta> Content-Security-Policy')
} else {
  const content = meta[1].replaceAll('&#39;', "'").replaceAll('&quot;', '"')
  if (content !== metaContentSecurityPolicy(policy)) {
    findings.push(`index.html <meta> CSP differs from _headers: ${content}`)
  }
  const firstResource = index.search(/<(?:script|link)\b/i)
  if (firstResource !== -1 && firstResource < meta.index) {
    findings.push('index.html <meta> CSP comes after a script or stylesheet')
  }
}

// 4. Security headers on every HTML route, and the cache rules.
const directives = new Map(
  policy
    .split(';')
    .map((directive) => directive.trim().split(/\s+/))
    .filter((parts) => parts[0])
    .map(([name, ...values]) => [name.toLowerCase(), values.join(' ')]),
)
const expectDirective = (name, value) => {
  if (directives.get(name) !== value) findings.push(`_headers CSP: ${name} is not ${value}`)
}
expectDirective('default-src', "'none'")
expectDirective('script-src', "'self'")
expectDirective('connect-src', "'self'")
expectDirective('frame-ancestors', "'none'")
expectDirective('base-uri', "'none'")
if (/'unsafe-|\bdata:|\*/.test(policy)) {
  findings.push(`_headers CSP allows unsafe-*, data: or a wildcard: ${policy}`)
}

/** HSTS for at least a year. */
const ONE_YEAR_SECONDS = 31_536_000
const REQUIRED = {
  'content-security-policy': (value) => value === policy,
  'strict-transport-security': (value) =>
    Number(/^max-age=(\d+)/.exec(value)?.[1] ?? 0) >= ONE_YEAR_SECONDS,
  'x-content-type-options': (value) => value === 'nosniff',
  'referrer-policy': (value) => value !== '',
  'permissions-policy': (value) =>
    /\bcamera=\(\)/.test(value) && /\bgeolocation=\(\)/.test(value),
}
for (const route of ['/', '/index.html', '/explore', '/about', '/analysis/estimate']) {
  const headers = headersFor(route)
  for (const [name, valid] of Object.entries(REQUIRED)) {
    if (!valid(headers.get(name) ?? ''))
      findings.push(`_headers: ${route} lacks a valid ${name}`)
  }
}

const cacheControl = (path) => headersFor(path).get('cache-control') ?? '(default)'
for (const name of assetNames) {
  if (!/\bimmutable\b/.test(cacheControl(`/assets/${name}`))) {
    findings.push(`_headers: /assets/${name} is not cached as immutable`)
  }
}
for (const path of ['/', '/index.html', '/snap/current.json']) {
  if (cacheControl(path) !== 'no-cache') {
    findings.push(`_headers: ${path} must be Cache-Control: no-cache, is ${cacheControl(path)}`)
  }
}
if (!/\bimmutable\b/.test(cacheControl('/snap/0123456789ab-0123456789abcdef/manifest.json'))) {
  findings.push('_headers: snapshot directories are not cached as immutable')
}

if (findings.length > 0) {
  process.stderr.write(`check-bundle: ${dist} is not deployable:\n  ${findings.join('\n  ')}\n`)
  if (findings.some((finding) => finding.startsWith('development API address'))) {
    process.stderr.write('Build without VITE_API_BASE_URL (same-origin /api) for deployment.\n')
  }
  process.exit(1)
}
process.stdout.write(
  `check-bundle: ${scripts.length} bundle file(s) with no development API address; ` +
    `${htmlFiles.length} HTML file(s) with no inline code and the <meta> CSP; ` +
    `_headers with ${rules.length} rules.\n`,
)
