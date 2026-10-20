import { chromium } from '@playwright/test'

export const BASE = 'http://127.0.0.1:47311'

function sortKeys(v) {
  if (Array.isArray(v)) return v.map(sortKeys)
  if (v && typeof v === 'object') {
    const o = {}
    for (const k of Object.keys(v).sort()) o[k] = sortKeys(v[k])
    return o
  }
  return v
}
export function specUrl(op, spec) {
  return `/analysis/${op}?spec=${Buffer.from(JSON.stringify(sortKeys(spec))).toString('base64url')}`
}
export const ESTIMATE_SPEC = {
  measure: 'average_minutes_per_day',
  activity: { include: ['0101'], exclude: [], label: 'Sleeping' },
  years: [2025],
  population: { age_min: 25, age_max: 54 },
  weights: 'multiyear',
  variance: 'replicate',
  confidence_level: 0.95,
}
export const TREND_SPEC = {
  measure: 'average_minutes_per_day',
  activity: { preset: 'leisure_and_sports_bls_table' },
  years: Array.from({ length: 23 }, (_, i) => 2003 + i),
}
export const COMPARE_SPEC = {
  measure: 'average_minutes_per_day',
  activity: { preset: 'household_activities_bls_table' },
  years: [2025],
  group_a: { has_household_children: true },
  group_b: { has_household_children: false },
  label_a: 'Household children present',
  label_b: 'No household children',
}

export async function control(body) {
  const r = await fetch(`${BASE}/__control`, { method: 'POST', body: JSON.stringify(body) })
  if (!r.ok) throw new Error('control failed')
}
export async function serverLog() {
  return (await fetch(`${BASE}/__log`)).json()
}

export async function launch() {
  const browser = await chromium.launch()
  return browser
}

/** Installs CSP-violation + console capture on a context. */
export async function instrument(context, sink) {
  await context.addInitScript(() => {
    window.__csp = []
    document.addEventListener('securitypolicyviolation', (e) => {
      window.__csp.push({
        directive: e.violatedDirective,
        blocked: e.blockedURI,
        sample: e.sample,
        source: e.sourceFile + ':' + e.lineNumber,
      })
    })
  })
  context.on('console', (m) => sink.push({ type: m.type(), text: m.text() }))
  context.on('weberror', (e) => sink.push({ type: 'pageerror', text: String(e.error()) }))
}

export async function mainText(page) {
  return page.evaluate(() => {
    const m = document.querySelector('main')
    return (m ? m.innerText : document.body.innerText).replace(/\s+/g, ' ').trim().slice(0, 600)
  })
}
