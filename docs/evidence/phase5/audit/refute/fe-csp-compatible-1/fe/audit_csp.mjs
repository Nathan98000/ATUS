// Runtime CSP check of the production build under a strict policy (no 'unsafe-inline', no 'unsafe-eval').
import { BASE, COMPARE_SPEC, ESTIMATE_SPEC, TREND_SPEC, control, instrument, launch, mainText, specUrl } from './audit_common.mjs'

const out = { pages: [] }
await control({ rules: {}, meta_patch: null, clear_log: true })
const browser = await launch()
const context = await browser.newContext({ baseURL: BASE })
const consoleSink = []
await instrument(context, consoleSink)
const page = await context.newPage()
const policy = []
page.on('response', (r) => {
  if (r.url() === BASE + '/' || r.url().startsWith(BASE + '/explore'))
    policy.push(r.headers()['content-security-policy'])
})

async function visit(path, extra) {
  await page.goto(path)
  await page.waitForLoadState('networkidle')
  if (extra) await extra()
  await page.waitForTimeout(500)
  const csp = await page.evaluate(() => window.__csp)
  out.pages.push({ path: path.slice(0, 60), csp, text: (await mainText(page)).slice(0, 160) })
}

await visit('/')
await visit('/explore', async () => {
  // open activity picker, advanced methodology, switch to compare & trend
  const change = page.getByRole('button', { name: 'Change activity' })
  if (await change.count()) await change.click()
  await page.getByText('Advanced methodology').click()
  await page.getByRole('radio', { name: 'Compare two groups' }).check({ force: true })
  await page.locator('summary', { hasText: 'Shared filters' }).click()
  await page.getByRole('radio', { name: 'Trend over time' }).check({ force: true })
})
await visit(specUrl('estimate', ESTIMATE_SPEC), async () => {
  await page.getByText('View exact values').click().catch(() => {})
  await page.getByRole('button', { name: 'Share analysis' }).click()
})
await visit(specUrl('trend', TREND_SPEC), async () => {
  const chart = page.locator('[tabindex="0"]').first()
  await chart.focus().catch(() => {})
  await page.keyboard.press('ArrowRight')
  await page.getByText('View data').click().catch(() => {})
})
await visit(specUrl('compare', COMPARE_SPEC))
await visit('/about')
await visit('/no-such-page')
await visit('/analysis/estimate?spec=garbage')

out.policyHeaderSeen = policy[0]
out.analysisKeyShown = null
await page.goto(specUrl('estimate', ESTIMATE_SPEC))
await page.waitForLoadState('networkidle')
out.analysisKeyShown = await page.getByText('Analysis key').count()
out.console = consoleSink
await browser.close()
console.log(JSON.stringify(out, null, 2))
