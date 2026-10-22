import { QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import type { CompareRequest, EstimateRequest } from '../api/types'
import { decodeSpec, encodeSpec } from '../domain/urlSpec'
import { COLD_START_BUDGET_MS } from '../api/resilience'
import { LocationProbe, testQueryClient, withFakeClock } from '../test/render'
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
  await waitFor(() =>
    expect(screen.getByRole('button', { name: 'Analyze' })).toBeInTheDocument(),
  )
}

describe('builder defaults and discovery', () => {
  it('offers a supported default analysis using the latest year from /meta', async () => {
    renderExplore()
    await waitForBuilder()
    expect(
      await screen.findByText('Sleeping', { selector: '.activity-picker__name' }),
    ).toBeInTheDocument()
    expect(screen.getByLabelText('Year')).toHaveValue('2025')
    expect(screen.getByRole('radio', { name: /Average time per day/ })).toBeChecked()
  })

  it('summarizes the analysis beside the form, with the Analyze button', async () => {
    const user = userEvent.setup()
    renderExplore()
    await waitForBuilder()
    const summary = screen.getByRole('region', { name: 'Your analysis' })
    const detail = (term: string) => within(summary).getByText(term).nextElementSibling
    expect(detail('Doing what')).toHaveTextContent('Sleeping')
    expect(detail('For whom')).toHaveTextContent('All respondents (age 15+)')
    expect(detail('When')).toHaveTextContent('2025')
    expect(detail('Measured as')).toHaveTextContent('Average time per day')
    expect(detail('Type')).toHaveTextContent('One number')
    expect(within(summary).getByRole('button', { name: 'Analyze' })).toBeInTheDocument()

    // It follows every edit.
    await user.selectOptions(screen.getByLabelText('Sex'), 'female')
    expect(detail('For whom')).toHaveTextContent('Women')
    await user.click(screen.getByRole('radio', { name: 'Compare two groups' }))
    // A comparison starts as men vs women; the estimate's "Women" filter
    // would contradict it, so it is not carried over.
    expect(detail('For whom')).toHaveTextContent(/^Men vs Women$/)
    expect(detail('Type')).toHaveTextContent('Comparison of two groups')
  })

  it('builds measure choices from the capability metadata', async () => {
    renderExplore()
    await waitForBuilder()
    for (const label of [
      /Average time per day/,
      /Share of people who do it/,
      /Time among those who do it/,
      /Number of people who do it/,
    ]) {
      expect(screen.getByRole('radio', { name: label })).toBeInTheDocument()
    }
  })

  it('shows a clear failure state when the API stays unreachable past the cold-start budget', async () => {
    // Intentional Phase 5 change: an unreachable API is retried for the
    // cold-start budget (a sleeping free host looks exactly like this) before
    // the failure is shown.
    await withFakeClock(async () => {
      server.use(http.get(api('/meta'), () => HttpResponse.error()))
      renderExplore()
      await vi.advanceTimersByTimeAsync(COLD_START_BUDGET_MS)
      const alert = await screen.findByRole('alert')
      expect(alert).toHaveTextContent('could not be reached')
      expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
    })
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

  it('configures and submits a comparison by one dimension', async () => {
    const user = userEvent.setup()
    renderExplore()
    await waitForBuilder()
    await user.click(screen.getByRole('radio', { name: 'Compare two groups' }))

    // One choice and two values, not two sets of filters.
    expect(screen.getByLabelText('Compare by')).toHaveValue('sex')
    expect(screen.queryByRole('group', { name: 'Group A' })).not.toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Compare by'), 'employment_status')
    expect(screen.getByLabelText('First group')).toHaveValue('employed')
    expect(screen.getByLabelText('Second group')).toHaveValue('not_in_labor_force')
    await user.selectOptions(screen.getByLabelText('Second group'), 'unemployed')

    await user.click(screen.getByRole('button', { name: 'Analyze' }))
    const location = screen.getByTestId('location').textContent ?? ''
    expect(location).toMatch(/^\/analysis\/compare\?spec=/)
    const spec = decodeSpec(location.split('spec=')[1] as string, 'compare') as CompareRequest
    expect(spec.group_a).toEqual({ employment_status: 'employed' })
    expect(spec.group_b).toEqual({ employment_status: 'unemployed' })
    expect(spec.label_a).toBe('Employed')
    expect(spec.label_b).toBe('Unemployed')
  })

  it('compares age groups as bands, the oldest open-ended', async () => {
    const user = userEvent.setup()
    renderExplore()
    await waitForBuilder()
    await user.click(screen.getByRole('radio', { name: 'Compare two groups' }))
    await user.selectOptions(screen.getByLabelText('Compare by'), 'age')
    await user.click(screen.getByRole('button', { name: 'Analyze' }))
    const location = screen.getByTestId('location').textContent ?? ''
    const spec = decodeSpec(location.split('spec=')[1] as string, 'compare') as CompareRequest
    expect(spec.group_a).toEqual({ age_min: 15, age_max: 24 })
    expect(spec.group_b).toEqual({ age_min: 65 })
    expect([spec.label_a, spec.label_b]).toEqual(['Ages 15–24', 'Age 65+'])
  })

  it('refuses to compare a group with itself', async () => {
    const user = userEvent.setup()
    renderExplore()
    await waitForBuilder()
    await user.click(screen.getByRole('radio', { name: 'Compare two groups' }))
    await user.selectOptions(screen.getByLabelText('Second group'), 'male')
    await user.click(screen.getByRole('button', { name: 'Analyze' }))
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Choose two different groups to compare.',
    )
    expect(screen.getByTestId('location').textContent).toBe('/explore')
  })

  it('builds custom groups with the full filters, starting from the current pair', async () => {
    const user = userEvent.setup()
    renderExplore()
    await waitForBuilder()
    await user.click(screen.getByRole('radio', { name: 'Compare two groups' }))
    await user.selectOptions(screen.getByLabelText('Compare by'), 'custom')

    const groupA = screen.getByRole('group', { name: 'Group A' })
    const groupB = screen.getByRole('group', { name: 'Group B' })
    expect(within(groupA).getByLabelText('Sex')).toHaveValue('male')
    expect(within(groupA).getByLabelText('Name')).toHaveValue('Men')
    await user.selectOptions(within(groupB).getByLabelText('Employment'), 'employed')
    await user.clear(within(groupB).getByLabelText('Name'))
    await user.type(within(groupB).getByLabelText('Name'), 'Working women')

    await user.click(screen.getByRole('button', { name: 'Analyze' }))
    const location = screen.getByTestId('location').textContent ?? ''
    const spec = decodeSpec(location.split('spec=')[1] as string, 'compare') as CompareRequest
    expect(spec.group_a).toEqual({ sex: 'male' })
    expect(spec.group_b).toEqual({ sex: 'female', employment_status: 'employed' })
    expect(spec.label_b).toBe('Working women')
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
    expect(
      screen.getByText('Watching TV', { selector: '.activity-picker__name' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /Share of people who do it/ })).toBeChecked()
    expect(screen.getByLabelText('Year')).toHaveValue('2024')
    expect(screen.getByLabelText('Sex')).toHaveValue('female')
  })

  /** base64url of `spec` exactly as written (wrong types included). */
  function rawSpec(spec: object): string {
    return btoa(JSON.stringify(spec))
      .replaceAll('+', '-')
      .replaceAll('/', '_')
      .replace(/=+$/, '')
  }
  const UNUSABLE = /could not be opened in the builder/

  // Review round 2: wrongly typed fields crashed the page (an object preset,
  // include list or label reached React as a child) or made Analyze throw
  // (a numeric label_a), and a malformed link silently became the default.
  it.each([
    ['estimate', { activity: { preset: { a: 1 } }, years: [2023] }],
    ['estimate', { activity: { include: [{ a: 1 }] }, years: [2023] }],
    ['estimate', { activity: { include: ['01'], label: { a: 1 } }, years: [2023] }],
    [
      'compare',
      {
        activity: { preset: 'sleep' },
        group_a: { sex: 'male' },
        group_b: { sex: 'female' },
        label_a: 7,
        label_b: 'B',
        years: [2023],
      },
    ],
    [
      'compare',
      {
        activity: { preset: 'sleep' },
        group_a: 'men',
        group_b: { sex: 'female' },
        years: [2023],
      },
    ],
  ] as const)(
    'an unusable %s prefill link says so and starts from the default',
    async (op, spec) => {
      const user = userEvent.setup()
      renderExplore(`/explore?op=${op}&spec=${rawSpec(spec)}`)
      await waitForBuilder()
      expect(screen.getByText(UNUSABLE)).toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: 'Analyze' }))
      expect(screen.getByText('analysis page')).toBeInTheDocument()
      const location = screen.getByTestId('location').textContent ?? ''
      expect(decodeSpec(location.split('spec=')[1] as string, 'estimate').activity).toEqual({
        preset: 'sleep',
      })
    },
  )

  it('a usable prefill link shows no such note', async () => {
    renderExplore(
      `/explore?op=estimate&spec=${rawSpec({ activity: { preset: 'sleep' }, years: [2023] })}`,
    )
    await waitForBuilder()
    expect(screen.queryByText(UNUSABLE)).not.toBeInTheDocument()
  })
})

describe('confidence levels from a shared analysis (review round 3)', () => {
  // The builder offers 90/95/99%, but a hand-made link can carry any level
  // the API accepts. The select showed "90%" while Analyze sent 0.975.
  it('shows a prefilled 97.5% level as 97.5%, sends it unchanged, and still offers 90%', async () => {
    const user = userEvent.setup()
    const spec: EstimateRequest = {
      activity: { preset: 'sleep' },
      years: [2025],
      confidence_level: 0.975,
    }
    renderExplore(`/explore?op=estimate&spec=${encodeSpec(spec)}`)
    await waitForBuilder()
    const select = screen.getByLabelText('Confidence level') as HTMLSelectElement
    expect(select.selectedOptions[0]).toHaveTextContent('97.5%')
    expect(
      within(select)
        .getAllByRole('option')
        .map((option) => option.textContent),
    ).toEqual(['90%', '95%', '97.5%', '99%'])

    await user.click(screen.getByRole('button', { name: 'Analyze' }))
    const location = screen.getByTestId('location').textContent ?? ''
    const sent = decodeSpec(location.split('spec=')[1] as string, 'estimate') as EstimateRequest
    expect(sent.confidence_level).toBe(0.975)
  })

  it('after a prefilled 97.5% level, choosing 90% really selects and sends 0.9', async () => {
    // In a browser, picking the option already shown fires no change: before
    // the fix "90%" was shown for 0.975, so a real 90% was unreachable. jsdom
    // fires change regardless, so the test above (97.5% is what is shown) is
    // the regression check; this one pins that the extra option gives way.
    const user = userEvent.setup()
    const spec: EstimateRequest = {
      activity: { preset: 'sleep' },
      years: [2025],
      confidence_level: 0.975,
    }
    renderExplore(`/explore?op=estimate&spec=${encodeSpec(spec)}`)
    await waitForBuilder()
    const select = screen.getByLabelText('Confidence level') as HTMLSelectElement
    await user.selectOptions(select, '90%')
    expect(select.selectedOptions[0]).toHaveTextContent('90%')

    await user.click(screen.getByRole('button', { name: 'Analyze' }))
    const location = screen.getByTestId('location').textContent ?? ''
    const sent = decodeSpec(location.split('spec=')[1] as string, 'estimate') as EstimateRequest
    expect(sent.confidence_level).toBe(0.9)
  })

  it('a standard level adds no extra option', async () => {
    renderExplore()
    await waitForBuilder()
    const select = screen.getByLabelText('Confidence level') as HTMLSelectElement
    expect(select.selectedOptions[0]).toHaveTextContent('95%')
    expect(within(select).getAllByRole('option')).toHaveLength(3)
  })
})

describe('shared filters of a comparison (review round 3)', () => {
  // "Filters must never be active yet invisible": the comparison's shared
  // population restricts both groups, so it must not hide in a closed section.
  function sharedSection(): HTMLDetailsElement {
    return screen.getByText(/^Narrow down both groups/).closest('details') as HTMLDetailsElement
  }

  it('a prefilled shared filter opens its section and marks it active', async () => {
    const spec: CompareRequest = {
      activity: { preset: 'sleep' },
      years: [2025],
      population: { age_min: 65 },
      group_a: { sex: 'male' },
      group_b: { sex: 'female' },
      label_a: 'Men',
      label_b: 'Women',
    }
    renderExplore(`/explore?op=compare&spec=${encodeSpec(spec)}`)
    await waitForBuilder()
    const shared = sharedSection()
    expect(shared.open).toBe(true)
    expect(shared.querySelector('summary')).toHaveTextContent(
      'Narrow down both groups (active)',
    )
    expect(within(shared).getByLabelText('Minimum age')).toHaveValue(65)
    // Men vs women opens as the simple choice it is.
    expect(screen.getByLabelText('Compare by')).toHaveValue('sex')
  })

  it('a link whose groups are not one simple choice opens in the custom editors', async () => {
    const spec: CompareRequest = {
      activity: { preset: 'sleep' },
      years: [2025],
      group_a: { sex: 'male' },
      group_b: { sex: 'female' },
      label_a: 'Dads',
      label_b: 'Moms',
    }
    renderExplore(`/explore?op=compare&spec=${encodeSpec(spec)}`)
    await waitForBuilder()
    // Its own names stay visible and editable, never sent unseen.
    expect(screen.getByLabelText('Compare by')).toHaveValue('custom')
    const groupA = screen.getByRole('group', { name: 'Group A' })
    expect(within(groupA).getByLabelText('Name')).toHaveValue('Dads')
  })

  it('a shared filter on the compared dimension opens the custom editors', async () => {
    const spec: CompareRequest = {
      activity: { preset: 'sleep' },
      years: [2025],
      population: { sex: 'female' },
      group_a: { sex: 'male' },
      group_b: { sex: 'female' },
      label_a: 'Men',
      label_b: 'Women',
    }
    renderExplore(`/explore?op=compare&spec=${encodeSpec(spec)}`)
    await waitForBuilder()
    expect(screen.getByLabelText('Compare by')).toHaveValue('custom')
    expect(screen.getByText(/^Filters for both groups/).closest('details')?.open).toBe(true)
  })

  it('filters carried over from an estimate stay visible after switching to compare', async () => {
    const user = userEvent.setup()
    renderExplore()
    await waitForBuilder()
    await user.type(screen.getByLabelText('Minimum age'), '65')
    await user.click(screen.getByRole('radio', { name: 'Compare two groups' }))
    const shared = sharedSection()
    expect(shared.open).toBe(true)
    expect(shared.querySelector('summary')).toHaveTextContent('(active)')
    expect(within(shared).getByLabelText('Minimum age')).toHaveValue(65)

    await user.click(screen.getByRole('button', { name: 'Analyze' }))
    const location = screen.getByTestId('location').textContent ?? ''
    const sent = decodeSpec(location.split('spec=')[1] as string, 'compare') as CompareRequest
    expect(sent.population).toEqual({ age_min: 65 })
  })

  it('editing the shared filter does not collapse its section under the cursor', async () => {
    const user = userEvent.setup()
    renderExplore()
    await waitForBuilder()
    await user.type(screen.getByLabelText('Minimum age'), '65')
    await user.click(screen.getByRole('radio', { name: 'Compare two groups' }))
    const shared = sharedSection()
    await user.clear(within(shared).getByLabelText('Minimum age'))
    expect(shared.open).toBe(true)
    expect(shared.querySelector('summary')).not.toHaveTextContent('(active)')
  })

  it('clearing it before the opening toggle event arrives does not collapse it either', async () => {
    // The browser delivers the toggle event of the section's programmatic
    // opening as a later task (Chromium sometimes only after the next edit).
    // Switching and clearing in one go, before that task runs, collapsed the
    // section under the cursor when "open" was learned from that event alone.
    const user = userEvent.setup()
    renderExplore()
    await waitForBuilder()
    await user.type(screen.getByLabelText('Minimum age'), '65')
    fireEvent.click(screen.getByRole('radio', { name: 'Compare two groups' }))
    const shared = sharedSection()
    expect(shared.open).toBe(true)
    fireEvent.change(within(shared).getByLabelText('Minimum age'), { target: { value: '' } })
    expect(shared.open).toBe(true)
    expect(shared.querySelector('summary')).not.toHaveTextContent('(active)')
    // …and it stays open once the late toggle event has been delivered.
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(shared.open).toBe(true)
  })

  it('stays collapsed and unmarked when no shared filter is set', async () => {
    const user = userEvent.setup()
    renderExplore()
    await waitForBuilder()
    await user.click(screen.getByRole('radio', { name: 'Compare two groups' }))
    const shared = sharedSection()
    expect(shared.open).toBe(false)
    expect(shared.querySelector('summary')?.textContent).toBe(
      'Narrow down both groups (optional)',
    )
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

describe('a trend split into groups (like the Shiny app)', () => {
  it('draws one line per group: the link names the split beside the spec', async () => {
    const user = userEvent.setup()
    renderExplore()
    await waitForBuilder()
    await user.click(screen.getByRole('radio', { name: 'Change over time' }))
    await user.selectOptions(screen.getByLabelText('Lines'), 'employment_status')
    const groups = screen.getByRole('group', { name: 'Show groups' })
    // Every group starts shown; the split dimension leaves the filters.
    expect(within(groups).getAllByRole('checkbox', { checked: true })).toHaveLength(3)
    expect(screen.queryByLabelText('Employment')).not.toBeInTheDocument()
    await user.click(within(groups).getByRole('checkbox', { name: 'Unemployed' }))
    const summary = screen.getByRole('region', { name: 'Your analysis' })
    expect(within(summary).getByText('For whom').nextElementSibling).toHaveTextContent(
      'Employed, Not in the labor force',
    )

    await user.click(screen.getByRole('button', { name: 'Analyze' }))
    const location = screen.getByTestId('location').textContent ?? ''
    expect(location).toMatch(
      /^\/analysis\/trend\?spec=[\w-]+&split=employment_status&groups=employed,not_in_labor_force$/,
    )
    // The spec itself is the plain trend, filter-free on the split dimension.
    const spec = decodeSpec(/spec=([\w-]+)/.exec(location)?.[1] as string, 'trend')
    expect(spec.population).toEqual({})
  })

  it('needs at least two groups to draw', async () => {
    const user = userEvent.setup()
    renderExplore()
    await waitForBuilder()
    await user.click(screen.getByRole('radio', { name: 'Change over time' }))
    await user.selectOptions(screen.getByLabelText('Lines'), 'sex')
    await user.click(screen.getByRole('checkbox', { name: 'Women' }))
    await user.click(screen.getByRole('button', { name: 'Analyze' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Choose at least two groups to show.')
    expect(screen.getByTestId('location').textContent).toBe('/explore')
  })

  it('reopens a split link with its groups', async () => {
    const spec: EstimateRequest = { activity: { preset: 'sleep' }, years: [2023, 2024, 2025] }
    renderExplore(`/explore?op=trend&spec=${encodeSpec(spec)}&split=age&groups=15-24,65%2B`)
    await waitForBuilder()
    expect(screen.getByLabelText('Lines')).toHaveValue('age')
    expect(screen.getByRole('checkbox', { name: 'Ages 15–24' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Age 65+' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Ages 25–34' })).not.toBeChecked()
  })

  it('a split this release does not offer opens the default analysis with a note', async () => {
    const spec: EstimateRequest = { activity: { preset: 'sleep' }, years: [2025] }
    renderExplore(`/explore?op=trend&spec=${encodeSpec(spec)}&split=sex&groups=male,other`)
    await waitForBuilder()
    expect(screen.getByText(/could not be opened in the builder/)).toBeInTheDocument()
  })
})
