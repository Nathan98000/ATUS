/**
 * Result-view coverage added from the adversarial review: variance='none'
 * presentation, proportion-unit comparisons (differences in percentage
 * points, never "%"), non-adjacent-year trend segmentation — and the
 * figures and small print each result's chart carries.
 */
import type { ReactElement } from 'react'
import { render as renderPlain, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import type { CompareResponse, EstimateResponse, TrendPoint } from '../../api/types'
import {
  compareChildrenFixture,
  estimateSleepFixture,
  estimateTvFixture,
  trendLeisureFixture,
} from '../../test/fixtures'
import { CompareView } from './CompareView'
import { EstimateView } from './EstimateView'
import { TrendChart, availableSegments } from './TrendChart'
import { TrendView } from './TrendView'

/** The views link to the About page ("What does 95% mean?"): a router around them. */
function render(ui: ReactElement) {
  return renderPlain(<MemoryRouter>{ui}</MemoryRouter>)
}

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
    render(<EstimateView result={noVariance} />)
    expect(screen.getByText(/No confidence interval is available/)).toBeInTheDocument()
    expect(screen.queryByText(/±/)).not.toBeInTheDocument()
    expect(screen.queryByText(/confidence\)/i)).not.toBeInTheDocument()
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
    render(<CompareView result={proportionCompare()} />)
    const difference = screen.getByTestId('difference')
    const { label_a: labelA } = compareChildrenFixture
    // The difference of two proportions is percentage points, never "%".
    // One data line, the interval read from the larger group's side
    // (review round 6).
    expect(difference).toHaveTextContent(
      `Difference: 3.2 percentage points higher for ${labelA} (95% confidence interval: 1.0 to 5.4 percentage points)`,
    )
    expect(difference.textContent).not.toMatch(/3\.2%/)
    // Group-level estimates remain percentages (beside their bars).
    expect(screen.getAllByText('45.0%')).toHaveLength(1)
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
      <TrendChart
        series={[{ label: 'All', points: points }]}
        unit="minutes_per_day"
        measureLabel="Average time per day"
      />,
    )
    const segments = screen.getAllByTestId('trend-line-segment')
    expect(segments.length).toBe(2)
    for (const segment of segments) {
      expect((segment.getAttribute('points') ?? '').split(' ').length).toBe(2)
    }
  })
})

describe('long group labels (review round 1)', () => {
  const chartLabels = (container: HTMLElement) =>
    Array.from(container.querySelectorAll('.compare-chart__label')).map(
      (label) => label.textContent ?? '',
    )

  it('the chart shows a long label whole (it wraps it)', () => {
    // label_a/label_b may be up to 80 characters (the API's limit). The
    // SVG chart cut them at 48; the HTML chart wraps them instead.
    const longA = 'M'.repeat(80)
    const result: CompareResponse = { ...compareChildrenFixture, label_a: longA }
    const { container } = render(<CompareView result={result} />)
    expect(chartLabels(container)).toEqual([longA, compareChildrenFixture.label_b])
    expect(screen.getByRole('img').getAttribute('aria-label')).toContain(longA)
  })

  it('two labels that share a long beginning stay distinct in the chart (review round 2)', () => {
    // Both were drawn as "Women 25-54, employed full time, with children…",
    // leaving only the colour to tell which estimate belongs to which group.
    const labelA = 'Women 25-54, employed full time, with children under 18'
    const labelB = 'Women 25-54, employed full time, with children under 6'
    const result: CompareResponse = {
      ...compareChildrenFixture,
      label_a: labelA,
      label_b: labelB,
    }
    const { container } = render(<CompareView result={result} />)
    expect(chartLabels(container)).toEqual([labelA, labelB])
  })

  it('two equal labels are told apart by their group in the chart', () => {
    const result: CompareResponse = {
      ...compareChildrenFixture,
      label_a: 'Parents',
      label_b: 'Parents',
    }
    const { container } = render(<CompareView result={result} />)
    expect(chartLabels(container)).toEqual(['Group A: Parents', 'Group B: Parents'])
  })

  it('distinct labels are drawn without a group prefix', () => {
    const { container } = render(<CompareView result={compareChildrenFixture} />)
    expect(chartLabels(container)).toEqual([
      compareChildrenFixture.label_a,
      compareChildrenFixture.label_b,
    ])
  })
})

describe('confidence levels other than 90/95/99% (review round 3)', () => {
  // The API accepts any level in (0.5, 1) from a hand-made link or request;
  // the page must name the level it computed.
  it('an estimate labels its 97.5% confidence interval as 97.5%, not 98%', () => {
    const result: EstimateResponse = {
      ...estimateSleepFixture,
      estimate: { ...estimateSleepFixture.estimate, confidence_level: 0.975 },
    }
    render(<EstimateView result={result} />)
    expect(screen.getByText(/97\.5% confidence/)).toBeInTheDocument()
    expect(screen.queryByText(/98%/)).not.toBeInTheDocument()
  })

  it("a trend's small print names the level of its band", () => {
    const result = {
      ...trendLeisureFixture,
      points: trendLeisureFixture.points.map((point) =>
        point.estimate == null
          ? point
          : { ...point, estimate: { ...point.estimate, confidence_level: 0.975 } },
      ),
    }
    render(<TrendView result={result} />)
    expect(screen.getByText(/Shaded band: 97\.5% confidence interval\./)).toBeInTheDocument()
    expect(screen.queryByText(/98%/)).not.toBeInTheDocument()
  })

  it('a 99.9% interval is never called a 100% interval', () => {
    const result: EstimateResponse = {
      ...estimateSleepFixture,
      estimate: { ...estimateSleepFixture.estimate, confidence_level: 0.999 },
    }
    render(<EstimateView result={result} />)
    expect(screen.getByText(/99\.9% confidence/)).toBeInTheDocument()
    expect(screen.queryByText(/100%/)).not.toBeInTheDocument()
  })

  it("a comparison's difference names its 95.45% level", () => {
    const result: CompareResponse = {
      ...compareChildrenFixture,
      difference: { ...compareChildrenFixture.difference, confidence_level: 0.9545 },
    }
    render(<CompareView result={result} />)
    expect(screen.getByTestId('difference')).toHaveTextContent('95.45% confidence interval:')
  })
})

describe('estimate figures', () => {
  it('shows a duration as a stretch of a 24-hour day', () => {
    // 528.8 minutes: 36.7% of the day.
    render(<EstimateView result={estimateSleepFixture} />)
    expect(screen.getByRole('img', { name: '8h 49m out of 24 hours' })).toBeInTheDocument()
    expect(screen.getByTestId('day-fill').style.getPropertyValue('--share')).toBe(
      String(estimateSleepFixture.estimate.value / 1440),
    )
    expect(screen.getByText(/95% confidence interval:/)).toHaveTextContent('8h 43m to 8h 55m')
    expect(screen.queryByText(/likely range/i)).not.toBeInTheDocument()
  })

  it('shows a rate as dots among a hundred people', () => {
    // 72.8%
    const { container } = render(<EstimateView result={estimateTvFixture} />)
    expect(
      screen.getByRole('img', { name: 'About 73 in every 100 people' }),
    ).toBeInTheDocument()
    expect(container.querySelectorAll('.hundred-grid__dot')).toHaveLength(100)
    expect(container.querySelectorAll('.hundred-grid__dot.is-filled')).toHaveLength(73)
  })

  it('names the sample on its own line, then the source and the graph’s author exactly', () => {
    render(<EstimateView result={estimateSleepFixture} />)
    // The diaries have their own line (review round 6: they broke the
    // owner's sentence in two).
    expect(screen.getByText("Based on 2,597 people's diaries.")).toBeInTheDocument()
    const source = screen.getByText(
      /Source: American Time Use Survey, U\.S\. Bureau of Labor Statistics/,
    )
    expect(source.textContent).toBe(
      'Source: American Time Use Survey, U.S. Bureau of Labor Statistics. Graph by Nathan Nguyen · X: @NathanDNguyen',
    )
    expect(screen.getByRole('link', { name: '@NathanDNguyen' })).toHaveAttribute(
      'href',
      'https://x.com/NathanDNguyen',
    )
    // The harmonized-codes note is not shown at all (review round 5).
    expect(screen.queryByText(/coded the same way/)).not.toBeInTheDocument()
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
  })
})

describe('how sure, in plain words (review round 6)', () => {
  it('links the interval to the About page’s explanation', () => {
    render(<EstimateView result={estimateSleepFixture} />)
    expect(screen.getByRole('link', { name: 'What does 95% mean?' })).toHaveAttribute(
      'href',
      '/about#how-sure',
    )
  })

  it('a comparison’s key says the whiskers are each group’s interval', () => {
    render(<CompareView result={compareChildrenFixture} />)
    expect(screen.getByText(/Thin lines:/)).toHaveTextContent(
      "Thin lines: each group's 95% confidence interval.",
    )
    expect(screen.getByText(/^Diaries behind each bar:/, { selector: 'p' })).toBeInTheDocument()
  })

  it('says a figure from few diaries is too few to rely on, and why 0 to 0 (D7)', () => {
    const few: EstimateResponse = {
      ...estimateSleepFixture,
      n_respondents: 13,
      n_participants: 0,
      estimate: {
        value: 0,
        unit: 'minutes_per_day',
        standard_error: 0,
        confidence_level: 0.95,
        ci_lower: 0,
        ci_upper: 0,
      },
    }
    render(<EstimateView result={few} combine={{ path: '/analysis/x', years: '2023–2025' }} />)
    const note = screen.getByRole('note')
    expect(note).toHaveTextContent(
      "Only 13 people's diaries are behind this figure, too few to rely on. With so few, the 95% confidence interval can understate the uncertainty. None of them did this on their diary day, which is why the interval is 0 to 0.",
    )
    expect(
      screen.getByRole('link', { name: 'Combine 2023–2025 for more diaries' }),
    ).toHaveAttribute('href', '/analysis/x')
  })

  it('says nothing more for a figure from enough diaries', () => {
    render(
      <EstimateView
        result={estimateSleepFixture}
        combine={{ path: '/x', years: '2023–2025' }}
      />,
    )
    expect(screen.queryByText(/too few to rely on/)).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Combine/ })).not.toBeInTheDocument()
  })
})

describe('a duration under an hour (review round 6)', () => {
  const minutes = (value: number, lower: number, upper: number): EstimateResponse => ({
    ...estimateSleepFixture,
    estimate: {
      value,
      unit: 'minutes_per_day',
      standard_error: 1,
      confidence_level: 0.95,
      ci_lower: lower,
      ci_upper: upper,
    },
  })

  it('draws the first hour enlarged, with the interval as a whisker', () => {
    render(<EstimateView result={minutes(7.2, 6.1, 8.4)} />)
    expect(screen.getByTestId('day-hour')).toHaveTextContent('The first hour, enlarged')
    const whisker = screen.getByTestId('day-hour-whisker')
    expect(whisker.style.getPropertyValue('--from')).toBe(String(6.1 / 60))
    expect(whisker.style.getPropertyValue('--to')).toBe(String(8.4 / 60))
  })

  it('draws no whisker when the interval leaves the hour', () => {
    render(<EstimateView result={minutes(9.5, -7.8, 26.9)} />)
    expect(screen.getByTestId('day-hour')).toBeInTheDocument()
    expect(screen.queryByTestId('day-hour-whisker')).not.toBeInTheDocument()
    // A negative end keeps its sign once (review round 6: "-1h -8m").
    expect(screen.getByText(/95% confidence interval:/)).toHaveTextContent('\u22128m to 27m')
  })

  it('needs no enlarged hour for longer durations', () => {
    render(<EstimateView result={estimateSleepFixture} />)
    expect(screen.queryByTestId('day-hour')).not.toBeInTheDocument()
  })
})
