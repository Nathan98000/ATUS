/**
 * Brief §15 (ADR-013, "New ATUS year procedure"): the frontend picks up a new
 * survey year through `/meta` alone. A future release — code 0326, survey
 * years 2003–2026, its own data version — must reach every year control, the
 * defaults, the home page and the result pages with no code change, and the
 * 2020 rules must still hold.
 *
 * The 0326 metadata is the captured 0325 `/meta` with only `data.release`,
 * `data.years` and the data version changed, as the next multi-year file
 * changes them. Responses for 2026 are captured 0325 responses relabelled
 * 2026: the page only displays what the API computed, so their numbers do
 * not matter here.
 */
import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import type {
  AnalysisRequest,
  EstimateRequest,
  EstimateResponse,
  MetaResponse,
  Operation,
  TrendRequest,
  TrendResponse,
} from '../api/types'
import { prewarmedAnalyses } from '../domain/prewarm'
import { decodeSpec, encodeSpec, explorePath } from '../domain/urlSpec'
import {
  activitiesFixture,
  estimatePandemicFixture,
  estimateSleepFixture,
  metaFixture,
  populationMetadataFixture,
  presetsFixture,
  trendLeisureFixture,
} from '../test/fixtures'
import { testQueryClient } from '../test/render'
import { analysisHeaders, api, installSnapshot, server } from '../test/server'

const YEARS_0326 = Array.from({ length: 2026 - 2003 + 1 }, (_, index) => 2003 + index)
const DATA_VERSION_0326 = '0326:serving-20270915T120000Z-0a1b2c3d:00112233aabb'

const meta0326: MetaResponse = {
  ...metaFixture,
  data: { ...metaFixture.data, release: '0326', years: YEARS_0326 },
}

const sleep2026: EstimateRequest = {
  measure: 'average_minutes_per_day',
  activity: { preset: 'sleep' },
  years: [2026],
  population: { age_min: 25, age_max: 54 },
  weights: 'multiyear',
  variance: 'replicate',
  confidence_level: 0.95,
}

/** The captured 2025 sleep estimate, relabelled 2026. */
const estimate2026: EstimateResponse = {
  ...estimateSleepFixture,
  years: [2026],
  spec: { ...estimateSleepFixture.spec, years: [2026] },
}

/** The captured 2003–2025 leisure trend (2020 unavailable), extended by a 2026 point. */
const point2025 = trendLeisureFixture.points.find((point) => point.year === 2025)
if (point2025 === undefined) throw new Error('the trend fixture lost its 2025 point')
const trend0326: TrendResponse = {
  ...trendLeisureFixture,
  points: [...trendLeisureFixture.points, { ...point2025, year: 2026 }],
  spec: { ...trendLeisureFixture.spec, years: YEARS_0326 },
}

/** Headers a 0326 deployment sends with every analysis. */
const headers0326 = () => ({ ...analysisHeaders(), 'X-Data-Version': DATA_VERSION_0326 })

/** Serves the 0326 release; returns the bodies of the analyses the page sends. */
function serve0326(): { operation: string; body: AnalysisRequest }[] {
  const posted: { operation: string; body: AnalysisRequest }[] = []
  server.use(
    http.get(api('/meta'), () =>
      HttpResponse.json(meta0326, { headers: { 'X-Data-Version': DATA_VERSION_0326 } }),
    ),
    http.post(api('/analysis/estimate'), async ({ request }) => {
      posted.push({ operation: 'estimate', body: (await request.json()) as AnalysisRequest })
      return HttpResponse.json(estimate2026, { headers: headers0326() })
    }),
    http.post(api('/analysis/trend'), async ({ request }) => {
      posted.push({ operation: 'trend', body: (await request.json()) as AnalysisRequest })
      return HttpResponse.json(trend0326, { headers: headers0326() })
    }),
  )
  return posted
}

function renderApp(route: string, client = testQueryClient()) {
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return client
}

async function waitForBuilder() {
  expect(await screen.findByRole('button', { name: 'Analyze' })).toBeInTheDocument()
}

function optionValues(select: HTMLElement): number[] {
  return within(select)
    .getAllByRole('option')
    .map((option) => Number((option as HTMLOptionElement).value))
}

/** The spec behind an in-app analysis link. */
function linkSpec(link: HTMLElement, operation: Operation): AnalysisRequest {
  const href = link.getAttribute('href') ?? ''
  expect(href.startsWith(`/analysis/${operation}?`), href).toBe(true)
  return decodeSpec(new URLSearchParams(href.split('?')[1]).get('spec') ?? '', operation)
}

const PARTIAL_NOTE = /2020 is a partial-year estimate: the survey paused that spring/
const RELEASE_NOTICE = /different data release/

afterEach(() => {
  server.events.removeAllListeners()
})

describe('a new survey year arrives through /meta alone (§15)', () => {
  it('the explore page offers 2026 in every year control, and defaults to it', async () => {
    const user = userEvent.setup()
    serve0326()
    renderApp('/explore')
    await waitForBuilder()

    // Trend (the builder's default): the full period, 2003–2026.
    const from = screen.getByLabelText('From')
    const to = screen.getByLabelText('To')
    expect(from).toHaveValue('2003')
    expect(to).toHaveValue('2026')
    expect(optionValues(from)).toEqual(YEARS_0326)
    expect(optionValues(to)).toEqual(YEARS_0326)

    // Single year: every reported year, newest first, 2026 preselected.
    await user.click(screen.getByRole('radio', { name: 'One number' }))
    const year = screen.getByLabelText('Year')
    expect(year).toHaveValue('2026')
    expect(optionValues(year)).toEqual([...YEARS_0326].reverse())

    // Several years combined: one checkbox per reported year, starting from 2026.
    await user.click(screen.getByText('More options'))
    await user.click(screen.getByRole('checkbox', { name: /Combine several years/ }))
    const pooled = screen.getByRole('group', { name: 'Years to combine' })
    const boxes = within(pooled).getAllByRole('checkbox')
    expect(boxes.map((box) => Number(box.closest('label')?.textContent))).toEqual(YEARS_0326)
    expect(within(pooled).getByRole('checkbox', { name: '2026' })).toBeChecked()
    expect(within(pooled).getByRole('checkbox', { name: '2025' })).not.toBeChecked()
  })

  it('the default analyses request 2026: a trend of 2003–2026, an estimate of [2026]', async () => {
    const user = userEvent.setup()
    const posted = serve0326()
    renderApp('/explore')
    await waitForBuilder()
    // The builder opens on a trend (review round 4).
    await user.click(screen.getByRole('button', { name: 'Analyze' }))
    expect(await screen.findByTestId('point-2026')).toBeInTheDocument()
    // The trend and, alongside it, its 2020 under the pandemic weights (the
    // mock answers with the leisure trend, so the link's canonical form names
    // another activity and asks for that one's 2020 too).
    const trends = posted.filter((entry) => entry.operation === 'trend')
    expect(trends).toHaveLength(1)
    // Every reported year, 2020 included: the API, not the page, rules it out.
    expect(trends[0]?.body.years).toEqual(YEARS_0326)
    const partial = posted.filter((entry) => entry.operation === 'estimate')
    expect(partial.length).toBeGreaterThan(0)
    for (const entry of partial) {
      expect(entry.body).toMatchObject({ years: [2020], weights: 'pandemic' })
    }

    const before = posted.length
    await user.click(screen.getByRole('link', { name: 'Explore' }))
    await waitForBuilder()
    await user.click(screen.getByRole('radio', { name: 'One number' }))
    await user.click(screen.getByRole('button', { name: 'Analyze' }))
    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    const later = posted.slice(before)
    expect(later).toHaveLength(1)
    expect(later[0]?.operation).toBe('estimate')
    expect(later[0]?.body.years).toEqual([2026])
  })

  it('a trend through 2026 still shows 2020 as its partial-year point, and 2026 as a point', async () => {
    const posted = serve0326()
    const trend: TrendRequest = {
      measure: 'average_minutes_per_day',
      activity: { preset: 'leisure_and_sports_bls_table' },
      years: YEARS_0326,
    }
    renderApp(`/analysis/trend?spec=${encodeSpec(trend)}`)
    expect(await screen.findByTestId('point-2026')).toBeInTheDocument()
    expect(posted.map((entry) => entry.body.years)).toEqual(
      expect.arrayContaining([YEARS_0326, [2020]]),
    )
    expect(posted).toHaveLength(2)

    // 2020 from its partial-year estimate: a hollow point, reached by a
    // dotted connector between the two full-year runs.
    expect(screen.queryByTestId('unavailable-2020')).not.toBeInTheDocument()
    expect(screen.getByTestId('point-2020')).toHaveClass('is-partial')
    expect(screen.getByTestId('partial-note')).toHaveTextContent(
      '2020 (hollow point) is a partial year',
    )
    expect(screen.getAllByTestId('trend-line-segment')).toHaveLength(2)
    expect(screen.getAllByTestId('trend-line-connector')).toHaveLength(1)

    const table = screen.getByRole('table')
    const row = (label: string) =>
      within(table).getByRole('rowheader', { name: label }).closest('tr') as HTMLElement
    expect(row('2020 (partial year)')).not.toHaveTextContent('No data')
    expect(row('2026')).not.toHaveTextContent('No data')
  })

  it("the builder's 2020 note still follows the year and the weights", async () => {
    // A trend through 2020 says so on its chart, not in the builder (review
    // round 4); 2020 alone switches to its partial-year weights, with a note.
    const user = userEvent.setup()
    serve0326()
    renderApp('/explore')
    await waitForBuilder()
    expect(screen.queryByText(PARTIAL_NOTE)).not.toBeInTheDocument()

    await user.click(screen.getByRole('radio', { name: 'One number' }))
    await user.selectOptions(screen.getByLabelText('Year'), '2020')
    expect(screen.getByText(PARTIAL_NOTE)).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /2020 partial-year weights/ })).toBeChecked()
    await user.selectOptions(screen.getByLabelText('Year'), '2026')
    expect(screen.queryByText(PARTIAL_NOTE)).not.toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /Standard weights/ })).toBeChecked()
  })

  it('the home page and its starters follow the release, and the About page names it', async () => {
    serve0326()
    renderApp('/')
    const sleep = await screen.findByRole('link', { name: /How much do Americans sleep\?/ })
    expect(screen.getByText(/from 2003 to 2026/)).toBeInTheDocument()

    expect(linkSpec(sleep, 'estimate').years).toEqual([2026])
    const leisure = screen.getByRole('link', { name: /How has leisure time changed\?/ })
    expect(linkSpec(leisure, 'trend').years).toEqual(YEARS_0326)
    for (const question of [/Who watches more TV/, /Does household work differ/]) {
      expect(linkSpec(screen.getByRole('link', { name: question }), 'compare').years).toEqual([
        2026,
      ])
    }

    // The release is named on the About page, not in every page's footer
    // (review round 4).
    await userEvent
      .setup()
      .click(within(screen.getByRole('contentinfo')).getByRole('link', { name: 'About page' }))
    expect(await screen.findByText(/multi-year release/)).toHaveTextContent(
      /release 0326 \(\s*2003–2026/,
    )
  })

  it('a 2026 result carries the 0326 data version and offers the 2003–2026 trend', async () => {
    serve0326()
    const client = renderApp(`/analysis/estimate?spec=${encodeSpec(sleep2026)}`)
    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1 }).nextElementSibling).toHaveTextContent(
      /· 2026$/,
    )

    const trendLink = screen.getByRole('link', { name: 'View 2003–2026 trend' })
    expect(linkSpec(trendLink, 'trend').years).toEqual(YEARS_0326)

    // Results are cached under the 0326 version tag, and the page and its
    // results agree on the release: no reload notice.
    const keys = client
      .getQueryCache()
      .findAll({ queryKey: ['analysis'], predicate: (query) => query.state.data !== undefined })
      .map((query) => query.queryKey[2])
    expect(keys.length).toBeGreaterThan(0)
    expect(new Set(keys)).toEqual(new Set([`0.2:${DATA_VERSION_0326}`]))
    expect(screen.queryByText(RELEASE_NOTICE)).not.toBeInTheDocument()
  })

  it('a pandemic-weight 2020 estimate still offers no full-period trend', async () => {
    serve0326()
    server.use(
      http.post(api('/analysis/estimate'), () =>
        HttpResponse.json(estimatePandemicFixture, { headers: headers0326() }),
      ),
    )
    renderApp(
      `/analysis/estimate?spec=${encodeSpec(estimatePandemicFixture.spec as EstimateRequest)}`,
    )
    await screen.findByRole('img', { name: /out of 24 hours/ })
    expect(screen.queryByRole('link', { name: /trend$/ })).not.toBeInTheDocument()
  })

  it('a shared link through 2026 opens unchanged under 0326, and loses 2026 under 0325', async () => {
    const trend: TrendRequest = {
      measure: 'average_minutes_per_day',
      activity: { preset: 'sleep' },
      years: [2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026],
    }
    const route = explorePath('trend', trend)
    const dropped = /not in the loaded data release and were dropped/

    serve0326()
    const { unmount } = render(
      <QueryClientProvider client={testQueryClient()}>
        <MemoryRouter initialEntries={[route]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    await waitForBuilder()
    expect(screen.getByLabelText('From')).toHaveValue('2019')
    expect(screen.getByLabelText('To')).toHaveValue('2026')
    expect(screen.queryByText(dropped)).not.toBeInTheDocument()
    unmount()

    // The captured 0325 release has no 2026: the same link says so.
    server.resetHandlers()
    renderApp(route)
    await waitForBuilder()
    expect(screen.getByLabelText('From')).toHaveValue('2019')
    expect(screen.getByLabelText('To')).toHaveValue('2025')
    expect(screen.getByText(dropped)).toBeInTheDocument()
  })

  it('a 0326 static snapshot gives the deployed page 2026 without calling /meta', async () => {
    await installSnapshot({
      get: {
        '/api/v1/meta': meta0326,
        '/api/v1/population/metadata': populationMetadataFixture,
        '/api/v1/activities': activitiesFixture,
        '/api/v1/activities/presets': presetsFixture,
      },
      dataVersion: DATA_VERSION_0326,
    })
    let metaCalls = 0
    server.use(
      http.get(api('/meta'), () => {
        metaCalls += 1
        return HttpResponse.json(metaFixture)
      }),
      http.get(api('/wake'), () => HttpResponse.json({ origin: 'warm' })),
    )
    renderApp('/explore')
    await waitForBuilder()
    expect(screen.getByLabelText('To')).toHaveValue('2026')
    expect(metaCalls).toBe(0)
  })
})

describe('the prewarm list of a 0326 deploy (§15)', () => {
  const list = prewarmedAnalyses({
    years: YEARS_0326,
    presets: meta0326.capabilities.activity_presets,
    measures: meta0326.capabilities.measures.map((measure) => measure.name),
    weightSchemes: meta0326.capabilities.weight_schemes.map((scheme) => scheme.name),
    tier1: activitiesFixture.activities
      .filter((activity) => activity.level === 1)
      .map((activity) => ({ code: activity.code, name: activity.name })),
  })

  it('uses 2026 as the latest year and 2003–2026 as the full period', () => {
    // 130, plus the household starter as the builder re-sends it (review
    // round 2), plus the home page's chart (review round 4).
    expect(list).toHaveLength(135)
    for (const item of list) {
      const years = item.spec.years
      if (item.name.startsWith('pandemic weights:')) continue
      if (item.name.startsWith('home chart, 2020:')) {
        expect(years, item.name).toEqual([2020])
        continue
      }
      if (item.operation === 'trend') expect(years, item.name).toEqual(YEARS_0326)
      else expect(years, item.name).toEqual([2026])
    }
  })

  it('keeps the pandemic weights to 2019 and 2020', () => {
    const pandemic = list.filter((item) => item.name.startsWith('pandemic weights:'))
    expect(pandemic).toHaveLength(meta0326.capabilities.activity_presets.length * 2)
    for (const item of pandemic) {
      expect(item.spec.weights).toBe('pandemic')
      expect([[2019], [2020]]).toContainEqual(item.spec.years)
    }
    // Besides those, only the home chart's 2020 points use them.
    expect(
      list.filter(
        (item) =>
          item.spec.weights === 'pandemic' && !item.name.startsWith('home chart, 2020:'),
      ),
    ).toEqual(pandemic)
  })
})
