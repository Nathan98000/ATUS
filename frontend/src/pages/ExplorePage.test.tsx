import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router'
import { describe, expect, it } from 'vitest'

import type { CompareRequest, EstimateRequest } from '../api/types'
import { decodeSpec, encodeSpec } from '../domain/urlSpec'
import { LocationProbe, testQueryClient } from '../test/render'
import { metaFixture } from '../test/fixtures'
import { api, server } from '../test/server'
import { ExplorePage } from './ExplorePage'

function BackButton() {
  const navigate = useNavigate()
  return (
    <button type="button" onClick={() => navigate(-1)}>
      test-go-back
    </button>
  )
}

function renderExplore(route = '/explore') {
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path="/explore" element={<ExplorePage />} />
          <Route path="/analysis/:operation" element={<p>analysis page</p>} />
        </Routes>
        <BackButton />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

async function waitForBuilder() {
  await waitFor(() => expect(screen.getByText('Build an analysis')).toBeInTheDocument())
}

describe('builder defaults and discovery', () => {
  it('offers a supported default analysis using the latest year from /meta', async () => {
    renderExplore()
    await waitForBuilder()
    expect(await screen.findByText('Sleeping')).toBeInTheDocument()
    expect(screen.getByLabelText('Year')).toHaveValue('2025')
    expect(screen.getByRole('radio', { name: /Average time per day/ })).toBeChecked()
  })

  it('builds measure choices from the capability metadata', async () => {
    renderExplore()
    await waitForBuilder()
    for (const label of [
      /Average time per day/,
      /Participation rate/,
      /Time among participants/,
      /People per day/,
    ]) {
      expect(screen.getByRole('radio', { name: label })).toBeInTheDocument()
    }
  })

  it('shows a clear failure state when the API is unreachable', async () => {
    server.use(http.get(api('/meta'), () => HttpResponse.error()))
    renderExplore()
    const alert = await screen.findByRole('alert', {}, { timeout: 8000 })
    expect(alert).toHaveTextContent('could not be reached')
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })
})

describe('running an analysis', () => {
  it('commits the analysis to a shareable URL on Analyze', async () => {
    const user = userEvent.setup()
    renderExplore()
    await waitForBuilder()
    await user.click(screen.getByRole('button', { name: 'Analyze' }))

    const location = screen.getByTestId('location').textContent ?? ''
    expect(location).toMatch(/^\/analysis\/estimate\?spec=/)
    const spec = decodeSpec(location.split('spec=')[1] as string, 'estimate') as EstimateRequest
    expect(spec).toEqual({
      measure: 'average_minutes_per_day',
      activity: { preset: 'sleep' },
      years: [2025],
      population: {},
      weights: 'multiyear',
      variance: 'replicate',
      confidence_level: 0.95,
    })
  })

  it('blocks Analyze with an accessible summary when validation fails', async () => {
    const user = userEvent.setup()
    renderExplore()
    await waitForBuilder()
    await user.type(screen.getByLabelText('Minimum age'), '90')
    await user.type(screen.getByLabelText('Maximum age'), '20')
    await user.click(screen.getByRole('button', { name: 'Analyze' }))

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Minimum age cannot exceed maximum age.',
    )
    expect(screen.getByTestId('location').textContent).toBe('/explore')
  })

  it('configures and submits a comparison', async () => {
    const user = userEvent.setup()
    renderExplore()
    await waitForBuilder()
    await user.click(screen.getByRole('radio', { name: 'Compare two groups' }))

    const groupA = screen.getByRole('group', { name: 'Group A' })
    const groupB = screen.getByRole('group', { name: 'Group B' })
    await user.selectOptions(within(groupA).getByLabelText('Sex'), 'male')
    await user.selectOptions(within(groupB).getByLabelText('Sex'), 'female')
    await user.clear(within(groupA).getByLabelText('Name'))
    await user.type(within(groupA).getByLabelText('Name'), 'Men')

    await user.click(screen.getByRole('button', { name: 'Analyze' }))
    const location = screen.getByTestId('location').textContent ?? ''
    expect(location).toMatch(/^\/analysis\/compare\?spec=/)
    const spec = decodeSpec(location.split('spec=')[1] as string, 'compare') as CompareRequest
    expect(spec.group_a).toEqual({ sex: 'male' })
    expect(spec.group_b).toEqual({ sex: 'female' })
    expect(spec.label_a).toBe('Men')
  })
})

describe('prefilling from a shared analysis', () => {
  it('reconstructs builder state from ?op & ?spec', async () => {
    const spec: EstimateRequest = {
      measure: 'participation_rate',
      activity: { include: ['120303', '120304'], label: 'Watching TV' },
      years: [2024],
      population: { sex: 'female' },
      weights: 'multiyear',
      variance: 'replicate',
      confidence_level: 0.95,
    }
    renderExplore(`/explore?op=estimate&spec=${encodeSpec(spec)}`)
    await waitForBuilder()
    expect(screen.getByText('Watching TV')).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /Participation rate/ })).toBeChecked()
    expect(screen.getByLabelText('Year')).toHaveValue('2024')
    expect(screen.getByLabelText('Sex')).toHaveValue('female')
  })
})

describe('browser history (review finding: Back must not reset the builder)', () => {
  it('Back from a result returns to a prefilled builder, not defaults', async () => {
    const user = userEvent.setup()
    renderExplore()
    await waitForBuilder()
    await user.selectOptions(screen.getByLabelText('Sex'), 'female')
    await user.click(screen.getByRole('button', { name: 'Analyze' }))
    expect(screen.getByText('analysis page')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'test-go-back' }))
    await waitForBuilder()
    // The configuration survived the round trip through history.
    expect(screen.getByLabelText('Sex')).toHaveValue('female')
    expect(screen.getByTestId('location').textContent).toMatch(/^\/explore\?op=estimate&spec=/)
  })
})

describe('capability discovery (review finding: no hard-coded measures)', () => {
  it('renders only the measures the API reports', async () => {
    server.use(
      http.get(api('/meta'), () =>
        HttpResponse.json({
          ...metaFixture,
          capabilities: {
            ...metaFixture.capabilities,
            measures: metaFixture.capabilities.measures.filter(
              (measure) => measure.name === 'average_minutes_per_day',
            ),
          },
        }),
      ),
    )
    renderExplore()
    await waitForBuilder()
    expect(screen.getByRole('radio', { name: /Average time per day/ })).toBeInTheDocument()
    expect(screen.queryByRole('radio', { name: /Participation rate/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('radio', { name: /People per day/ })).not.toBeInTheDocument()
  })

  it('ignores a future measure the UI does not know', async () => {
    server.use(
      http.get(api('/meta'), () =>
        HttpResponse.json({
          ...metaFixture,
          capabilities: {
            ...metaFixture.capabilities,
            measures: [
              ...metaFixture.capabilities.measures,
              {
                name: 'median_minutes_per_day',
                unit: 'minutes_per_day',
                description: 'future',
              },
            ],
          },
        }),
      ),
    )
    renderExplore()
    await waitForBuilder()
    expect(screen.queryByRole('radio', { name: /median/i })).not.toBeInTheDocument()
    // The known measures are unaffected.
    expect(screen.getByRole('radio', { name: /Average time per day/ })).toBeChecked()
  })
})
