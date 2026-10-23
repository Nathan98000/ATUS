// Error presentation + retry behaviour of the production build for cold-start / quota / overload shapes.
import { BASE, ESTIMATE_SPEC, control, instrument, launch, mainText, serverLog, specUrl } from './audit_common.mjs'

const SCENARIOS = {
  meta_503_db_unavailable_json: {
    rules: { meta: { status: 503, code: 'database_unavailable', message: 'The analytical database is currently unavailable. Try again shortly.' } },
  },
  meta_10s_pool_timeout_503_json: {
    rules: { meta: { status: 503, delay: 10, code: 'database_unavailable', message: 'The analytical database is currently unavailable. Try again shortly.' } },
  },
  meta_503_twice_then_ok: {
    rules: { meta: { status: 503, fail_times: 2, code: 'database_unavailable', message: 'x' } },
    only: ['explore'],
  },
  meta_503_html_proxy_page: { rules: { meta: { status: 503, body: 'html' } } },
  meta_502_html_proxy_page: { rules: { meta: { status: 502, body: 'html' } } },
  meta_429_html_worker_quota: { rules: { meta: { status: 429, body: 'html', headers: { 'Retry-After': '3600' } } } },
  analysis_429_json_retry_after: {
    rules: { estimate: { status: 429, code: 'rate_limited', message: 'Too many analyses; retry in 30 s.', headers: { 'Retry-After': '30' } } },
    only: ['deeplink'],
  },
  analysis_503_json_overloaded_retry_after: {
    rules: { estimate: { status: 503, code: 'service_overloaded', message: 'Busy; retry in 5 s.', headers: { 'Retry-After': '5' } } },
    only: ['deeplink'],
  },
  analysis_504_html_proxy_timeout: { rules: { estimate: { status: 504, body: 'html' } }, only: ['deeplink'] },
  analysis_503_db_unavailable_json: {
    rules: { estimate: { status: 503, code: 'database_unavailable', message: 'The analytical database is currently unavailable. Try again shortly.' } },
    only: ['deeplink'],
  },
  meta_api_version_v2: { rules: {}, meta_patch: { api_version: 'v2' } },
  meta_incompatible_shape_no_data: { rules: {}, meta_patch: { data: null } },
}

const which = process.env.ONLY ? process.env.ONLY.split(',') : Object.keys(SCENARIOS)
const browser = await launch()
const results = {}
for (const name of which) {
  const sc = SCENARIOS[name]
  await control({ rules: sc.rules, meta_patch: sc.meta_patch ?? null, clear_log: true })
  const targets = { home: '/', explore: '/explore', deeplink: specUrl('estimate', ESTIMATE_SPEC) }
  const res = {}
  const t0 = Date.now()
  await Promise.all(
    Object.entries(targets)
      .filter(([k]) => !sc.only || sc.only.includes(k))
      .map(async ([k, path]) => {
        const ctx = await browser.newContext({ baseURL: BASE })
        const sink = []
        await instrument(ctx, sink)
        const page = await ctx.newPage()
        await page.goto(path, { waitUntil: 'domcontentloaded' })
        // settle: alert visible, or home starters resolved, or result rendered, or 45 s
        const deadline = Date.now() + 45_000
        let settledAt = null
        while (Date.now() < deadline) {
          const alert = await page.getByRole('alert').count()
          const txt = await mainText(page)
          const done =
            alert > 0 ||
            /could not be reached|How much do Americans sleep/.test(txt) && k === 'home' ||
            /Build an analysis Doing what/.test(txt) ||
            /minutes per day/.test(txt) ||
            /Something went wrong/.test(await page.locator('body').innerText())
          if (done) {
            settledAt = (Date.now() - t0) / 1000
            break
          }
          await page.waitForTimeout(250)
        }
        await page.waitForTimeout(300)
        const alertText = (await page.getByRole('alert').count())
          ? (await page.getByRole('alert').first().innerText()).replace(/\s+/g, ' ')
          : null
        res[k] = {
          settledAt,
          alertText,
          tryAgainButton: await page.getByRole('button', { name: 'Try again' }).count(),
          text: (await page.locator('body').innerText()).replace(/\s+/g, ' ').slice(0, 300),
          home_starter:
            k === 'home' && (await page.locator('.home-examples').count())
              ? (await page.locator('.home-examples').innerText()).replace(/\s+/g, ' ').slice(0, 160)
              : undefined,
          footer: (await page.locator('footer').count()) ? (await page.locator('footer').innerText()).replace(/\s+/g, ' ').slice(-60) : null,
          console: sink.slice(0, 4),
        }
        await ctx.close()
      }),
  )
  const log = (await serverLog()).filter((l) => l.key && l.status === 'recv')
  res.requests = log.map((l) => `${l.t.toFixed(2)} ${l.method} ${l.key} #${l.n}`)
  results[name] = res
}
await browser.close()
console.log(JSON.stringify(results, null, 2))
