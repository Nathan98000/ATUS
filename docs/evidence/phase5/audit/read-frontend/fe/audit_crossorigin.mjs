import { chromium } from '@playwright/test'
const browser = await chromium.launch()
const page = await browser.newPage()
const spec = { measure: 'average_minutes_per_day', activity: { include: ['0101'], exclude: [], label: 'Sleeping' }, years: [2025], population: { age_min: 25, age_max: 54 }, weights: 'multiyear', variance: 'replicate', confidence_level: 0.95 }
const sortKeys = (v) => Array.isArray(v) ? v.map(sortKeys) : v && typeof v === 'object' ? Object.fromEntries(Object.keys(v).sort().map((k) => [k, sortKeys(v[k])])) : v
await page.goto('http://127.0.0.1:47313/analysis/estimate?spec=' + Buffer.from(JSON.stringify(sortKeys(spec))).toString('base64url'))
await page.getByText('minutes per day').first().waitFor()
const shown = await page.getByText('Analysis key').count()
// error path: 422 from the cross-origin API -> technical details request id?
await fetch('http://127.0.0.1:47312/__control', { method: 'POST', body: JSON.stringify({ rules: { estimate: { status: 422, code: 'insufficient_data', message: 'No respondents.' } } }) })
const p2 = await browser.newPage()
await p2.goto('http://127.0.0.1:47313/analysis/estimate?spec=' + Buffer.from(JSON.stringify(sortKeys({ ...spec, years: [2024] }))).toString('base64url'))
await p2.getByRole('alert').waitFor()
const technical = await p2.locator('details code').textContent()
console.log(JSON.stringify({ crossOriginAnalysisKeyRowShown: shown, crossOriginErrorTechnicalDetails: technical }, null, 2))
await fetch('http://127.0.0.1:47312/__control', { method: 'POST', body: JSON.stringify({ rules: {} }) })
await browser.close()
