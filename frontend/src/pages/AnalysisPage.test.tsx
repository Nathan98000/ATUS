/**
 * Integration tests: user-visible behavior of the shareable result page with
 * the API mocked at the HTTP layer (fixtures are real captured responses).
 */
import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { HttpResponse, delay, http } from 'msw'
import { MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'

import type { EstimateRequest } from '../api/types'
import { encodeSpec } from '../domain/urlSpec'
import {
  compareChildrenFixture,
  error2020Fixture,
  estimateSleepFixture,
  trendLeisureFixture,
} from '../test/fixtures'
import { LocationProbe, testQueryClient } from '../test/render'
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

function renderAnalysisRoute(route: string) {
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path="/analysis/:operation" element={<AnalysisPage />} />
        </Routes>
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
    // API warnings are visible:
    expect(screen.getByRole('note')).toHaveTextContent(/BLS-harmonized/)
    // Methodology comes from the response:
    expect(screen.getByText('How was this calculated?')).toBeInTheDocument()
    expect(screen.getByText(/TUFNWGTP \(multiyear\)/)).toBeInTheDocument()
  })

  it('rewrites the URL to the canonical spec from the response', async () => {
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
    // The data table lists 2020 as unavailable, never as zero.
    const row = screen.getByRole('rowheader', { name: '2020' }).closest('tr') as HTMLElement
    expect(row).toHaveTextContent('Unavailable')
    expect(row).not.toHaveTextContent('0.0')
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
    expect(await screen.findByTestId('difference')).toHaveTextContent('-11.1 min')
    expect(screen.getByTestId('difference')).toHaveTextContent('± 4.2 min')
    expect(screen.getByText('1h 52m')).toBeInTheDocument()
    expect(screen.getByText('2h 3m')).toBeInTheDocument()
    // Difference direction is explicit:
    expect(screen.getByTestId('difference')).toHaveTextContent(
      'Household children present − No household children',
    )
    expect(compareChildrenFixture.difference.value).toBeLessThan(0)
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
    server.use(http.post(api('/analysis/estimate'), () => HttpResponse.error()))
    renderAnalysisRoute(`/analysis/estimate?spec=${encodeSpec(sleepSpec)}`)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('could not be reached')
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })

  it('shows a helpful page for malformed share links', async () => {
    renderAnalysisRoute('/analysis/estimate?spec=%%%broken%%%')
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This analysis link cannot be opened',
    )
    expect(screen.getByRole('link', { name: /Build an analysis/ })).toBeInTheDocument()
  })

  it('rejects unknown analysis types in the URL', async () => {
    renderAnalysisRoute(`/analysis/median?spec=${encodeSpec(sleepSpec)}`)
    expect(await screen.findByRole('alert')).toHaveTextContent('unknown analysis type')
  })
})

describe('race safety', () => {
  it('a slow earlier analysis can never overwrite a newer one', async () => {
    // Two different specs: the first response is slow, the second fast.
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

    const first = renderAnalysisRoute(`/analysis/estimate?spec=${encodeSpec(slowSpec)}`)
    await waitFor(() => expect(calls).toBe(1))
    first.unmount()
    renderAnalysisRoute(`/analysis/estimate?spec=${encodeSpec(sleepSpec)}`)

    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    // Give the slow response time to land; the shown value must not change.
    await new Promise((resolve) => setTimeout(resolve, 500))
    expect(screen.queryByText(/16h 39m/)).not.toBeInTheDocument()
    expect(screen.getByText('8h 49m')).toBeInTheDocument()
  })
})
