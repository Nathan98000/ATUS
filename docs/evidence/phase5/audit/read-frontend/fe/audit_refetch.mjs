// (9) Background /meta refetch failure after the 5-min staleTime; (10) data swap vs browser caches.
import { BASE, ESTIMATE_SPEC, control, launch, mainText, serverLog, specUrl } from './audit_common.mjs'

const DBDOWN = { meta: { status: 503, code: 'database_unavailable', message: 'The analytical database is currently unavailable. Try again shortly.' } }
const out = {}
const browser = await launch()
const count = (log, key) => log.filter((l) => l.key === key && l.status === 'recv').length
const focus = (page) => page.evaluate(() => window.dispatchEvent(new Event('visibilitychange')))

await control({ meta_cache: 'no-store' })
// ---- 9a: Explore builder, user edits, tab regains focus after >5 min while API/DB is failing
{
  await control({ rules: {}, meta_patch: null, clear_log: true })
  const ctx = await browser.newContext({ baseURL: BASE })
  const page = await ctx.newPage()
  await page.clock.install()
  await page.goto('/explore')
  await page.getByRole('heading', { name: 'Build an analysis' }).waitFor()
  await page.getByLabel('Minimum age').fill('30')
  await page.getByLabel('Maximum age').fill('40')
  await control({ rules: DBDOWN, clear_log: true })
  await page.clock.fastForward('06:00')
  await focus(page)
  for (let i = 0; i < 8; i++) { await page.clock.fastForward(1000); await page.waitForTimeout(150) }
  await page.waitForTimeout(500)
  const afterFailure = (await mainText(page)).slice(0, 200)
  const builderStillMounted = await page.getByLabel('Minimum age').count()
  const metaRequestsDuringOutage = count(await serverLog(), 'meta')
  await control({ rules: {}, clear_log: true })
  let ageAfterRecovery = null
  if (await page.getByRole('button', { name: 'Try again' }).count()) {
    await page.getByRole('button', { name: 'Try again' }).click()
    await page.getByLabel('Minimum age').waitFor({ timeout: 10_000 })
    ageAfterRecovery = await page.getByLabel('Minimum age').inputValue()
  }
  out['9a_explore_background_refetch_failure'] = { afterFailure, builderStillMounted, metaRequestsDuringOutage, ageBefore: '30', ageAfterRecovery }
  await ctx.close()
}

// ---- 9b: displayed result + background /meta refetch failure
{
  await control({ rules: {}, meta_patch: null, clear_log: true })
  const ctx = await browser.newContext({ baseURL: BASE })
  const page = await ctx.newPage()
  await page.clock.install()
  await page.goto(specUrl('estimate', ESTIMATE_SPEC))
  await page.getByText('minutes per day').first().waitFor()
  const before = (await mainText(page)).slice(0, 160)
  await control({ rules: DBDOWN, clear_log: true })
  await page.clock.fastForward('06:00')
  await focus(page)
  for (let i = 0; i < 8; i++) { await page.clock.fastForward(1000); await page.waitForTimeout(150) }
  await page.waitForTimeout(500)
  out['9b_result_page_background_refetch_failure'] = {
    before,
    after: (await mainText(page)).slice(0, 260),
    resultStillVisible: await page.getByText('minutes per day').count(),
    alert: (await page.getByRole('alert').count()) ? (await page.getByRole('alert').first().innerText()).replace(/\s+/g, ' ') : null,
  }
  await ctx.close()
}

await control({ meta_cache: 'public, max-age=300' })
// ---- 10: data swap (ingestion run 1 -> 2) while a tab is open
{
  await control({ rules: {}, meta_patch: null, clear_log: true })
  const ctx = await browser.newContext({ baseURL: BASE })
  const page = await ctx.newPage()
  await page.clock.install()
  const url = specUrl('estimate', ESTIMATE_SPEC)
  await page.goto(url)
  await page.getByText('minutes per day').first().waitFor()
  const log0 = await serverLog()
  const r = { initial: { meta: count(log0, 'meta'), estimatePOST: count(log0, 'estimate') } }

  // data swap on the server
  await control({ meta_patch: { data: { ingestion_run_id: 2 } }, clear_log: true })
  // client-side navigation away and back (SPA, no reload)
  await page.getByRole('link', { name: /ATUS\s*Explorer/ }).first().click()
  await page.getByRole('heading', { name: /Explore how Americans/ }).waitFor()
  await page.goBack()
  await page.getByText('minutes per day').first().waitFor()
  await page.waitForTimeout(500)
  let log = await serverLog()
  r.after_swap_back_navigation_within_5min = { meta: count(log, 'meta'), estimatePOST: count(log, 'estimate') }

  // >5 min later (JS clock) + focus: TanStack refetches /meta; does it reach the server (HTTP max-age=300, real time)?
  await control({ clear_log: true })
  await page.clock.fastForward('06:00')
  await focus(page)
  await page.waitForTimeout(1500)
  log = await serverLog()
  r.after_focus_refetch_real_time_lt_300s = { meta: count(log, 'meta'), estimatePOST: count(log, 'estimate') }

  // full reload within 300 s of the first /meta: is /meta served from the browser HTTP cache?
  await control({ clear_log: true })
  await page.reload()
  await page.getByText('minutes per day').first().waitFor()
  await page.waitForTimeout(500)
  log = await serverLog()
  r.after_reload = { meta: count(log, 'meta'), estimatePOST: count(log, 'estimate'), population: count(log, 'population'), presets: count(log, 'presets') }

  // new tab, same browser context (shared HTTP cache)
  await control({ clear_log: true })
  const p2 = await ctx.newPage()
  await p2.goto('/about')
  await p2.getByText('This instance serves').waitFor()
  await p2.waitForTimeout(300)
  log = await serverLog()
  r.new_tab_about = { meta: count(log, 'meta') }
  out['10_data_swap'] = r
  await ctx.close()
}
await browser.close()
console.log(JSON.stringify(out, null, 2))
