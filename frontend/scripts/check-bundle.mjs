#!/usr/bin/env node
/**
 * Fails when a built bundle still points at a development API (audit F24):
 * a loopback address with a port (`localhost:8000`, `127.0.0.1:8137`,
 * `[::1]:8000`). A production build must call its own origin (relative
 * /api/v1) unless VITE_API_BASE_URL deliberately names another one.
 *
 * react-router uses a bare `http://localhost` (no port) internally as a URL
 * parsing base; that is not an address the app calls, so it is not matched.
 *
 * Usage: node scripts/check-bundle.mjs [distDir]   (default: dist)
 */
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

const DEV_API_ADDRESS = /(?:localhost|127\.0\.0\.1|\[::1\]):\d+/g

const dist = process.argv[2] ?? 'dist'
const assets = join(dist, 'assets')
let files
try {
  files = readdirSync(assets).filter((name) => name.endsWith('.js'))
} catch (error) {
  process.stderr.write(`check-bundle: cannot read ${assets}: ${error.message}\n`)
  process.exit(2)
}

const findings = []
for (const name of files) {
  const text = readFileSync(join(assets, name), 'utf8')
  for (const match of text.matchAll(DEV_API_ADDRESS)) findings.push(`${name}: ${match[0]}`)
}

if (findings.length > 0) {
  process.stderr.write(
    `check-bundle: development API addresses in the bundle:\n  ${findings.join('\n  ')}\n` +
      'Build without VITE_API_BASE_URL (same-origin /api) for deployment.\n',
  )
  process.exit(1)
}
process.stdout.write(`check-bundle: ${files.length} bundle file(s), no development API address.\n`)
