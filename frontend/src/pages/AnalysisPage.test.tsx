/**
 * Integration tests: user-visible behavior of the shareable result page with
 * the API mocked at the HTTP layer (fixtures are real captured responses).
 */
import { QueryClientProvider } from '@tanstack/react-query'
import userEvent from '@testing-library/user-event'
import { render, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, delay, http } from 'msw'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import type { CompareRequest, EstimateRequest } from '../api/types'
import { encodeSpec } from '../domain/urlSpec'
import {
  compareChildrenFixture,
  error2020Fixture,
  estimatePandemicFixture,
  estimateSleepFixture,
  trendLeisureFixture,
} from '../test/fixtures'
import { Link } from 'react-router'

import { COLD_START_BUDGET_MS } from '../api/resilience'
import { LocationProbe, testQueryClient, withFakeClock } from '../test/render'
import { analysisHeaders, api, server } from '../test/server'
import { AnalysisPage } from './AnalysisPage'

const sleepSpec: EstimateRequest = {
  measure: 'average_minutes_per_day',
  activity: { preset: 'sleep' },
  years: [2025],
  population: { age_min: 25, age_max: 54 },
  weights: 'multiyear',
  variance: 'replicate',
  confidence_level: 0.95,
}

function renderAnalysisRoute(
  route: string,
  {
    client = testQueryClient(),
    navTo,
  }: { client?: ReturnType<typeof testQueryClient>; navTo?: string } = {},
) {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path="/analysis/:operation" element={<AnalysisPage />} />
        </Routes>
        {navTo ? <Link to={navTo}>go-to-next</Link> : null}
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('estimate results', () => {
  it('shows the estimate, uncertainty, samples, warnings, and methodology', async () => {
    renderAnalysisRoute(`/analysis/estimate?spec=${encodeSpec(sleepSpec)}`)

    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    expect(screen.getByText('528.8 minutes per day')).toBeInTheDocument()
    expect(screen.getByText(/± 2.9 min/)).toBeInTheDocument()
    expect(screen.getByText(/523.0 to 534.5 min/)).toBeInTheDocument()
    // Unweighted sample vs weighted population, clearly distinguished:
    expect(screen.getByText(/2,597 respondents/)).toBeInTheDocument()
    expect(screen.getByText(/131.1 million people on an average day/)).toBeInTheDocument()
    // API warnings are visible, in plain language:
    expect(screen.getByRole('note')).toHaveTextContent(
      /coded the same way in every survey year/,
    )
    // Methodology comes from the response, with the API's notes verbatim:
    expect(screen.getByText('How was this calculated?')).toBeInTheDocument()
    expect(screen.getByText(/TUFNWGTP \(multiyear\)/)).toBeInTheDocument()
    expect(screen.getByText('Technical notes').nextElementSibling).toHaveTextContent(
      /BLS-harmonized/,
    )
  })

  it('heads the result with the statistic, the activity, and the rest said once', async () => {
    renderAnalysisRoute(`/analysis/estimate?spec=${encodeSpec(sleepSpec)}`)
    await screen.findByText('8h 49m')
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(/^Sleeping$/)
    expect(screen.getByText('Average time per day')).toHaveClass('eyebrow')
    expect(screen.getByText('Ages 25–54 · 2025')).toBeInTheDocument()
  })

  it('rewrites the URL to the canonical spec from the response without refetching', async () => {
    let posts = 0
    server.use(
      http.post(api('/analysis/estimate'), () => {
        posts += 1
        return HttpResponse.json(estimateSleepFixture, { headers: analysisHeaders() })
      }),
    )
    // Request written with unsorted years/keys; response carries the canonical spec.
    const uncanonical = { ...sleepSpec, years: [2025] }
    renderAnalysisRoute(`/analysis/estimate?spec=${encodeSpec(uncanonical)}`)
    await screen.findByText('8h 49m')
    await waitFor(() => {
      const location = screen.getByTestId('location').textContent ?? ''
      expect(location).toBe(
        `/analysis/estimate?spec=${encodeSpec(estimateSleepFixture.spec as EstimateRequest)}`,
      )
    })
    // The canonical result was seeded into the cache — no second request.
    await waitFor(() => expect(screen.getByText('8h 49m')).toBeInTheDocument())
    expect(posts).toBe(1)
  })

  it('rewrites trend URLs to the canonical spec too', async () => {
    const requestSpec: EstimateRequest = {
      activity: { preset: 'leisure_and_sports_bls_table' },
      years: trendLeisureFixture.points.map((point) => point.year),
    }
    renderAnalysisRoute(`/analysis/trend?spec=${encodeSpec(requestSpec)}`)
    await screen.findByTestId('unavailable-2020')
    await waitFor(() => {
      const location = screen.getByTestId('location').textContent ?? ''
      expect(location).toBe(
        `/analysis/trend?spec=${encodeSpec(trendLeisureFixture.spec as EstimateRequest)}`,
      )
    })
  })

  it('does not offer the full-period trend shortcut for pandemic-weighted estimates', async () => {
    server.use(
      http.post(api('/analysis/estimate'), () =>
        HttpResponse.json(estimatePandemicFixture, { headers: analysisHeaders() }),
      ),
    )
    const pandemicSpec = estimatePandemicFixture.spec as EstimateRequest
    renderAnalysisRoute(`/analysis/estimate?spec=${encodeSpec(pandemicSpec)}`)
    await screen.findByRole('note') // the pandemic warning banner
    expect(screen.queryByRole('link', { name: /trend/i })).not.toBeInTheDocument()
  })

  it('surfaces a /meta failure instead of spinning forever', async () => {
    // Intentional Phase 5 change: the failure surfaces once the cold-start
    // retry budget is spent, not after ~3 s.
    await withFakeClock(async () => {
      server.use(http.get(api('/meta'), () => HttpResponse.error()))
      renderAnalysisRoute(`/analysis/estimate?spec=${encodeSpec(sleepSpec)}`)
      await vi.advanceTimersByTimeAsync(COLD_START_BUDGET_MS)
      const alert = await screen.findByRole('alert')
      expect(alert).toHaveTextContent('could not be reached')
      expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
    })
  })

  it('sets a descriptive document title', async () => {
    renderAnalysisRoute(`/analysis/estimate?spec=${encodeSpec(sleepSpec)}`)
    await screen.findByText('8h 49m')
    expect(document.title).toBe('Sleeping — Ages 25–54 — 2025 · ATUS Explorer')
  })
})

describe('trend and compare results', () => {
  it('renders the trend with its 2020 gap and data table', async () => {
    const spec: EstimateRequest = {
      activity: { preset: 'leisure_and_sports_bls_table' },
      years: trendLeisureFixture.points.map((point) => point.year),
    }
    renderAnalysisRoute(`/analysis/trend?spec=${encodeSpec(spec)}`)
    expect(await screen.findByTestId('unavailable-2020')).toBeInTheDocument()
    expect(screen.getAllByTestId('trend-line-segment').length).toBe(2)
    // API warnings must be visible on trend pages too.
    expect(screen.getByRole('note')).toHaveTextContent(
      /coded the same way in every survey year/,
    )
    // The data table lists 2020 as having no estimate, never as zero.
    const row = screen.getByRole('rowheader', { name: '2020' }).closest('tr') as HTMLElement
    expect(row).toHaveTextContent('No estimate')
    expect(row).not.toHaveTextContent('0.0')
    // The heading drops the definition parenthetical from the title, not the page.
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(/^Leisure and sports$/)
    expect(
      screen.getByText(/^As defined in BLS table A-1 · All respondents/),
    ).toBeInTheDocument()
    // The answer leads: latest and first year in words.
    expect(
      screen.getByText(/^Average time per day on “Leisure and sports” was/),
    ).toBeInTheDocument()
  })

  it('renders both groups and the API-computed difference', async () => {
    const spec = {
      activity: { preset: 'household_activities_bls_table' },
      years: [2025],
      group_a: { has_household_children: true },
      group_b: { has_household_children: false },
      label_a: 'Household children present',
      label_b: 'No household children',
    }
    renderAnalysisRoute(`/analysis/compare?spec=${encodeSpec(spec)}`)
    const difference = await screen.findByTestId('difference')
    // Difference direction is explicit, in words: B is larger here.
    expect(compareChildrenFixture.difference.value).toBeLessThan(0)
    expect(difference).toHaveTextContent('11 minutes more per day for No household children')
    expect(difference).toHaveTextContent(
      'Exact difference (Household children present − No household children): -11.1 min, standard error 4.2 min',
    )
    expect(screen.getAllByText('1h 52m').length).toBeGreaterThan(0)
    expect(screen.getAllByText('2h 3m').length).toBeGreaterThan(0)
    expect(
      screen.getByText(
        'In 2025, average time per day on “Household activities” was 11 minutes longer for No household children than for Household children present.',
      ),
    ).toBeInTheDocument()
    // API warnings must be visible on compare pages too.
    expect(screen.getByRole('note')).toHaveTextContent(
      /both groups coming from the same survey/,
    )
    // No shared filters: the details line names only the definition and year.
    expect(screen.queryByText(/Both groups:/)).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(
      /^Household activities: Household children present vs No household children$/,
    )
    expect(screen.getByText('As defined in BLS table A-1 · 2025')).toBeInTheDocument()
  })

  it("names a comparison's shared population outside the collapsed panels (review round 3)", async () => {
    // A comparison of people aged 65+ was titled and summarised as if it
    // covered everyone: the shared filter appeared only inside the closed
    // methodology and exact-values panels.
    const spec: CompareRequest = {
      activity: { preset: 'household_activities_bls_table' },
      years: [2025],
      population: { age_min: 65 },
      group_a: { sex: 'male' },
      group_b: { sex: 'female' },
      label_a: 'Men',
      label_b: 'Women',
    }
    renderAnalysisRoute(`/analysis/compare?spec=${encodeSpec(spec)}`)
    await screen.findByTestId('difference')
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(/: Men vs Women$/)
    expect(document.title).toMatch(/Men vs Women — Age 65\+ — 2025 · ATUS Explorer$/)
    const line = screen.getByText(/Both groups: Age 65\+ · 2025$/)
    expect(line.closest('details')).toBeNull()
  })
})

describe('error handling', () => {
  it('shows the API explanation for the 2020 restriction', async () => {
    server.use(
      http.post(api('/analysis/estimate'), () =>
        HttpResponse.json(error2020Fixture, { status: 422 }),
      ),
    )
    renderAnalysisRoute(
      `/analysis/estimate?spec=${encodeSpec({ activity: { preset: 'sleep' }, years: [2020] })}`,
    )
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('not statistically supported')
    expect(alert).toHaveTextContent(/data collection was suspended/)
    expect(screen.getAllByRole('link', { name: 'Adjust analysis' }).length).toBeGreaterThan(0)
  })

  it('distinguishes a down backend from an invalid request', async () => {
    // Intentional Phase 5 change: an unreachable host is retried within the
    // cold-start budget before the failure is shown.
    await withFakeClock(async () => {
      server.use(http.post(api('/analysis/estimate'), () => HttpResponse.error()))
      renderAnalysisRoute(`/analysis/estimate?spec=${encodeSpec(sleepSpec)}`)
      await vi.advanceTimersByTimeAsync(COLD_START_BUDGET_MS)
      const alert = await screen.findByRole('alert')
      expect(alert).toHaveTextContent('could not be reached')
      expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
    })
  })

  it('shows a helpful page for malformed share links', async () => {
    renderAnalysisRoute('/analysis/estimate?spec=%%%broken%%%')
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This analysis link cannot be opened',
    )
    expect(screen.getByRole('link', { name: /Build an analysis/ })).toBeInTheDocument()
  })

  it('a link with a wrongly typed field is an invalid link, not the app error page (review round 2)', async () => {
    // `preset: 5` reached humanize() (replaceAll on a number) and the error
    // boundary replaced the page.
    for (const activity of [{ preset: 5 }, { preset: { a: 1 } }, { include: [{}] }]) {
      const json = JSON.stringify({
        activity,
        measure: 'average_minutes_per_day',
        years: [2023],
      })
      const encoded = btoa(json).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '')
      const { unmount } = renderAnalysisRoute(`/analysis/estimate?spec=${encoded}`)
      expect(await screen.findByRole('alert')).toHaveTextContent(
        'This analysis link cannot be opened',
      )
      unmount()
    }
  })

  it('a comparison link whose has_household_children is a string is an invalid link (review round 4)', async () => {
    // The API computes "false" as without household children; the page
    // titled the result "Both groups: With household children".
    let posts = 0
    server.use(
      http.post(api('/analysis/compare'), () => {
        posts += 1
        return HttpResponse.json(compareChildrenFixture)
      }),
    )
    const json = JSON.stringify({
      activity: { preset: 'sleep' },
      years: [2019, 2023],
      population: { has_household_children: 'false' },
      group_a: { sex: 'male' },
      group_b: { sex: 'female' },
    })
    const encoded = btoa(json).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '')
    renderAnalysisRoute(`/analysis/compare?spec=${encoded}`)
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This analysis link cannot be opened',
    )
    expect(screen.queryByText(/household children/i)).not.toBeInTheDocument()
    expect(posts).toBe(0)
  })

  it('rejects unknown analysis types in the URL', async () => {
    renderAnalysisRoute(`/analysis/median?spec=${encodeSpec(sleepSpec)}`)
    expect(await screen.findByRole('alert')).toHaveTextContent('unknown analysis type')
  })

  it('the heavy-only daily cap offers a shorter analysis (review round 2)', async () => {
    // The edge governor refuses only heavy analyses once 300 were admitted
    // today; a full-period trend is heavy, a single year is not.
    server.use(
      http.post(api('/analysis/trend'), () =>
        HttpResponse.json(
          {
            error: {
              code: 'daily_capacity_reached',
              message: 'Daily capacity reached.',
              details: { scope: 'daily', budget: 'heavy', resumes_at: '2026-10-22T00:00:00Z' },
            },
          },
          { status: 503, headers: { 'Retry-After': '3600' } },
        ),
      ),
    )
    const trendSpec = { ...sleepSpec, years: [2015, 2016, 2017, 2018, 2019, 2021, 2022, 2023] }
    renderAnalysisRoute(`/analysis/trend?spec=${encodeSpec(trendSpec)}`)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Today’s capacity for long analyses is used up')
    expect(alert).toHaveTextContent('Analyses of up to 7 years')
    expect(within(alert).getByRole('link', { name: 'Adjust analysis' })).toHaveAttribute(
      'href',
      expect.stringMatching(/^\/explore\?op=trend&spec=/),
    )
    expect(within(alert).queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
  })
})

describe('race safety', () => {
  it('a slow superseded analysis can never overwrite the newer one', async () => {
    // One page, one QueryClient: the user navigates from spec A (slow
    // response) to spec B (fast response) while A is still in flight.
    const slowSpec = { ...sleepSpec, population: {} }
    let calls = 0
    server.use(
      http.post(api('/analysis/estimate'), async () => {
        calls += 1
        if (calls === 1) {
          await delay(400)
          return HttpResponse.json(
            {
              ...estimateSleepFixture,
              estimate: { ...estimateSleepFixture.estimate, value: 999 },
            },
            { headers: analysisHeaders() },
          )
        }
        return HttpResponse.json(estimateSleepFixture, { headers: analysisHeaders() })
      }),
    )

    const user = userEvent.setup()
    renderAnalysisRoute(`/analysis/estimate?spec=${encodeSpec(slowSpec)}`, {
      navTo: `/analysis/estimate?spec=${encodeSpec(sleepSpec)}`,
    })
    await waitFor(() => expect(calls).toBe(1))
    await user.click(screen.getByRole('link', { name: 'go-to-next' }))

    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    // Let the slow response land; the displayed result must not change to
    // the superseded analysis's value (999 min = 16h 39m).
    await new Promise((resolve) => setTimeout(resolve, 500))
    expect(screen.queryByText(/16h 39m/)).not.toBeInTheDocument()
    expect(screen.getByText('8h 49m')).toBeInTheDocument()
  })
})
