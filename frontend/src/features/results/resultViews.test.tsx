/**
 * Result-view coverage added from the adversarial review: variance='none'
 * presentation, proportion-unit comparisons (differences in percentage
 * points, never "%"), and non-adjacent-year trend segmentation.
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { CompareResponse, EstimateResponse, TrendPoint } from '../../api/types'
import { compareChildrenFixture, estimateSleepFixture } from '../../test/fixtures'
import { CompareView } from './CompareView'
import { EstimateView } from './EstimateView'
import { TrendChart, availableSegments } from './TrendChart'

describe('variance="none" presentation', () => {
  it('says explicitly that no uncertainty was requested — never fabricates one', () => {
    const noVariance: EstimateResponse = {
      ...estimateSleepFixture,
      variance_method: 'none',
      estimate: {
        value: estimateSleepFixture.estimate.value,
        unit: estimateSleepFixture.estimate.unit,
        standard_error: null,
        confidence_level: null,
        ci_lower: null,
        ci_upper: null,
      },
    }
    render(<EstimateView result={noVariance} analysisKey={null} />)
    expect(screen.getByText(/No uncertainty was requested/)).toBeInTheDocument()
    expect(screen.queryByText(/±/)).not.toBeInTheDocument()
    expect(screen.queryByText(/confidence interval/i)).not.toBeInTheDocument()
  })
})

describe('proportion-unit comparisons', () => {
  function proportionCompare(): CompareResponse {
    const asProportion = (value: number, se: number): EstimateResponse => ({
      ...compareChildrenFixture.group_a,
      measure: 'participation_rate',
      estimate: {
        value,
        unit: 'proportion_of_population',
        standard_error: se,
        confidence_level: 0.95,
        ci_lower: value - 1.96 * se,
        ci_upper: value + 1.96 * se,
      },
    })
    return {
      ...compareChildrenFixture,
      measure: 'participation_rate',
      group_a: asProportion(0.45, 0.008),
      group_b: asProportion(0.418, 0.006),
      difference: {
        value: 0.032,
        unit: 'proportion_of_population',
        standard_error: 0.011,
        confidence_level: 0.95,
        ci_lower: 0.0104,
        ci_upper: 0.0536,
      },
    }
  }

  it('renders the difference and its CI in percentage points, matching the SE', () => {
    render(<CompareView result={proportionCompare()} analysisKey={null} />)
    const difference = screen.getByTestId('difference')
    // The difference of two proportions is percentage points, never "%".
    expect(difference).toHaveTextContent('+3.2 pp')
    expect(difference).toHaveTextContent('± 1.1 pp')
    expect(difference).toHaveTextContent('1.0 to 5.4 pp')
    expect(difference.textContent).not.toMatch(/\+3\.2%/)
    // Group-level estimates remain percentages.
    expect(screen.getByText('45.0%')).toBeInTheDocument()
  })
})

describe('non-adjacent trend years', () => {
  const point = (year: number, value: number): TrendPoint => ({
    year,
    estimate: { value, unit: 'minutes_per_day' },
    n_respondents: 100,
    n_participants: 90,
    weighted_population_per_day: 1e6,
    unavailable_reason: null,
  })

  it('never draws a line across a skipped year (no visual interpolation)', () => {
    // 2018-2019 then 2021-2022: 2020 was not requested, so the API returns
    // no point for it — the chart must still not paint across its position.
    const points = [point(2018, 300), point(2019, 310), point(2021, 305), point(2022, 300)]
    expect(availableSegments(points).map((segment) => segment.map((p) => p.year))).toEqual([
      [2018, 2019],
      [2021, 2022],
    ])
    render(
      <TrendChart points={points} unit="minutes_per_day" measureLabel="Average time per day" />,
    )
    const segments = screen.getAllByTestId('trend-line-segment')
    expect(segments.length).toBe(2)
    for (const segment of segments) {
      expect((segment.getAttribute('points') ?? '').split(' ').length).toBe(2)
    }
  })
})

describe('long group labels (review round 1)', () => {
  it('the chart draws at most 48 characters of a label; the cards keep it whole', () => {
    // label_a/label_b may be up to 80 characters (the API's limit), and SVG
    // text does not wrap: a long label ran off the chart, cut mid-glyph.
    const longA = 'M'.repeat(80)
    const result: CompareResponse = { ...compareChildrenFixture, label_a: longA }
    const { container } = render(<CompareView result={result} analysisKey={null} />)
    const chartLabels = Array.from(container.querySelectorAll('.chart-group-label'))
    expect(chartLabels[0]?.textContent).toBe(`${'M'.repeat(47)}…`)
    expect(chartLabels[1]?.textContent).toBe(compareChildrenFixture.label_b)
    expect(screen.getByRole('heading', { name: longA })).toBeInTheDocument()
    expect(screen.getByRole('img').getAttribute('aria-label')).toContain(longA)
  })

  it('two labels that share their first 47 characters stay distinct in the chart (review round 2)', () => {
    // Both were drawn as "Women 25-54, employed full time, with children…",
    // leaving only the colour to tell which estimate belongs to which group.
    const labelA = 'Women 25-54, employed full time, with children under 18'
    const labelB = 'Women 25-54, employed full time, with children under 6'
    const result: CompareResponse = {
      ...compareChildrenFixture,
      label_a: labelA,
      label_b: labelB,
    }
    const { container } = render(<CompareView result={result} analysisKey={null} />)
    const chartLabels = Array.from(container.querySelectorAll('.chart-group-label')).map(
      (label) => label.textContent ?? '',
    )
    expect(chartLabels).toHaveLength(2)
    expect(chartLabels[0]).not.toBe(chartLabels[1])
    expect(chartLabels[0]).toMatch(/^Group A: Women 25-54/)
    expect(chartLabels[1]).toMatch(/^Group B: Women 25-54/)
    for (const label of chartLabels) expect(Array.from(label).length).toBeLessThanOrEqual(48)
  })

  it('a comparison names the analytics version that computed it (review round 2)', () => {
    // The page's footer names the version of the page's /meta; each result
    // names its own, the comparison too (its methodology panel).
    const result: CompareResponse = {
      ...compareChildrenFixture,
      analytics_version: '0.3',
      group_a: { ...compareChildrenFixture.group_a, analytics_version: '0.3' },
      group_b: { ...compareChildrenFixture.group_b, analytics_version: '0.3' },
    }
    render(<CompareView result={result} analysisKey={null} />)
    const term = screen.getByText('Analytics version')
    expect(term.nextElementSibling).toHaveTextContent('0.3')
  })

  it('labels that are distinct once shortened are drawn without a group prefix', () => {
    const { container } = render(
      <CompareView result={compareChildrenFixture} analysisKey={null} />,
    )
    const chartLabels = Array.from(container.querySelectorAll('.chart-group-label'))
    expect(chartLabels[0]?.textContent).toBe(compareChildrenFixture.label_a)
    expect(chartLabels[1]?.textContent).toBe(compareChildrenFixture.label_b)
  })
})
