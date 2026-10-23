// Emulates a host via Playwright request interception (no network port, no server).
// Scenarios: fresh shell; stale shell whose entry bundle is gone with (a) SPA fallback
// returning index.html 200 text/html, (b) plain 404.
import { chromium } from '$REPO/frontend/node_modules/playwright/index.mjs'
import fs from 'node:fs'
import path from 'node:path'
const SITE = process.argv[2]
const realIndex = fs.readFileSync(path.join(SITE, 'index.html'), 'utf8')
const staleIndex = realIndex.replace(/\/assets\/index-[A-Za-z0-9_-]+\.js/, '/assets/index-OLDHASH0.js')
const types = { '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.html': 'text/html' }

async function run(name, shell, missingMode) {
  const browser = await chromium.launch()
  const ctx = await browser.newContext()
  const log = []
  await ctx.route('**/*', async (route) => {
    const u = new URL(route.request().url())
    if (u.hostname !== 'audit.test') { log.push(`ABORT ${u.href}`); return route.abort() }
    const p = u.pathname
    const file = path.join(SITE, p)
    if (p !== '/' && !p.endsWith('/') && fs.existsSync(file) && fs.statSync(file).isFile() && p !== '/index.html') {
      log.push(`200 ${p} (asset)`)
      return route.fulfill({ status: 200, contentType: types[path.extname(p)] || 'application/octet-stream', body: fs.readFileSync(file) })
    }
    if (p === '/' || p === '/index.html' || route.request().resourceType() === 'document') {
      log.push(`200 ${p} (shell)`)
      return route.fulfill({ status: 200, contentType: 'text/html', body: shell })
    }
    if (missingMode === 'spa') { log.push(`200 ${p} (SPA fallback -> index.html, text/html)`); return route.fulfill({ status: 200, contentType: 'text/html', body: shell }) }
    log.push(`404 ${p}`)
    return route.fulfill({ status: 404, contentType: 'text/plain', body: 'not found' })
  })
  const page = await ctx.newPage()
  const consoleMsgs = []
  page.on('console', (m) => consoleMsgs.push(`[${m.type()}] ${m.text()}`))
  page.on('pageerror', (e) => consoleMsgs.push(`[pageerror] ${e.message}`))
  await page.goto('http://audit.test/explore', { waitUntil: 'load' })
  await page.waitForTimeout(1500)
  const rootLen = await page.evaluate(() => document.getElementById('root')?.innerHTML.length ?? -1)
  const bodyText = (await page.evaluate(() => document.body.innerText)).trim().slice(0, 200)
  console.log(`=== ${name}`)
  console.log(`requests:\n  ${log.join('\n  ')}`)
  console.log(`console:\n  ${consoleMsgs.join('\n  ')}`)
  console.log(`#root innerHTML length: ${rootLen}`)
  console.log(`visible body text: ${JSON.stringify(bodyText)}`)
  await browser.close()
}
await run('A fresh shell, current bundle', realIndex, 'spa')
await run('B stale shell, bundle missing, SPA fallback (index.html 200)', staleIndex, 'spa')
await run('C stale shell, bundle missing, 404', staleIndex, '404')
