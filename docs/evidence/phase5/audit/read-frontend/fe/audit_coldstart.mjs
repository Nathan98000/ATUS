// Cold start: every metadata GET is held for DELAY seconds (as a sleeping host holds requests
// while it spins up). Observe Home, Explore and a deep-linked analysis over time.
import { BASE, ESTIMATE_SPEC, control, instrument, launch, mainText, serverLog, specUrl } from './audit_common.mjs'

const DELAY = Number(process.env.DELAY ?? 60)
const SNAPS = (process.env.SNAPS ?? '1,5,15,30,55,63,70').split(',').map(Number)
await control({
  rules: {
    meta: { delay: DELAY },
    population: { delay: DELAY },
    activities: { delay: DELAY },
    presets: { delay: DELAY },
  },
  meta_patch: null,
  clear_log: true,
})
const browser = await launch()
const targets = { home: '/', explore: '/explore', deeplink: specUrl('estimate', ESTIMATE_SPEC) }
const pages = {}
const consoleSink = []
for (const [name, path] of Object.entries(targets)) {
  const ctx = await browser.newContext({ baseURL: BASE })
  await instrument(ctx, consoleSink)
  pages[name] = { page: await ctx.newPage(), path }
}
await control({ clear_log: true })
const t0 = Date.now()
await Promise.all(Object.values(pages).map(({ page, path }) => page.goto(path, { waitUntil: 'domcontentloaded' })))
const snaps = []
for (const s of SNAPS) {
  const wait = t0 + s * 1000 - Date.now()
  if (wait > 0) await new Promise((r) => setTimeout(r, wait))
  const row = { t: s }
  for (const [name, { page }] of Object.entries(pages)) {
    row[name] = {
      text: (await mainText(page)).slice(0, 260),
      footer: (await page.locator('footer').innerText()).replace(/\s+/g, ' ').slice(-90),
      alerts: await page.getByRole('alert').count(),
      starter: name === 'home' ? (await page.locator('.home-examples').innerText()).replace(/\s+/g, ' ').slice(0, 200) : undefined,
    }
  }
  snaps.push(row)
}
const log = await serverLog()
await browser.close()
console.log(JSON.stringify({ DELAY, snaps, serverLog: log.filter((l) => l.key), console: consoleSink }, null, 2))
