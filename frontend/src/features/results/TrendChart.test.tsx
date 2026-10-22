/**
 * The 2020-gap behavior is a correctness requirement: unavailable years must
 * render as a labeled gap — no connecting line, no interpolation, no zero.
 */
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import type { TrendPoint } from '../../api/types'
import { trendLeisureFixture } from '../../test/fixtures'
import { TrendChart, availableSegments } from './TrendChart'

const points = trendLeisureFixture.points

describe('availableSegments', () => {
  it('splits the real 2003–2025 leisure trend at the unavailable 2020', () => {
    const segments = availableSegments(points)
    expect(segments.length).toBe(2)
    expect(segments[0]?.map((point) => point.year)).toEqual([
      2003, 2004, 2005, 2006, 2007, 2008, 2009, 2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017,
      2018, 2019,
    ])
    expect(segments[1]?.map((point) => point.year)).toEqual([2021, 2022, 2023, 2024, 2025])
  })

  it('never invents a value for an unavailable year', () => {
    const flattened = availableSegments(points).flat()
    expect(flattened.some((point) => point.year === 2020)).toBe(false)
    expect(flattened.every((point) => Number.isFinite(point.value))).toBe(true)
  })
})

describe('TrendChart 2020 gap rendering', () => {
  it('draws one line segment per available run — never across the gap', () => {
    render(
      <TrendChart
        series={[{ label: 'All', points: points }]}
        unit="minutes_per_day"
        measureLabel="Average time per day"
      />,
    )
    const segments = screen.getAllByTestId('trend-line-segment')
    expect(segments.length).toBe(2)
    // No polyline may contain x-coordinates on both sides of 2020.
    const gapX = Number(/cx="([\d.]+)"/.exec(screen.getByTestId('point-2019').outerHTML)?.[1])
    const afterX = Number(/cx="([\d.]+)"/.exec(screen.getByTestId('point-2021').outerHTML)?.[1])
    for (const segment of segments) {
      const xs = (segment.getAttribute('points') ?? '')
        .split(' ')
        .map((pair) => Number(pair.split(',')[0]))
      const spansGap = Math.min(...xs) <= gapX && Math.max(...xs) >= afterX
      expect(spansGap).toBe(false)
    }
  })

  it('marks the unavailable year with a labeled band', () => {
    render(
      <TrendChart
        series={[{ label: 'All', points: points }]}
        unit="minutes_per_day"
        measureLabel="Average time per day"
      />,
    )
    // The explanation is in the chart's small print (TrendView).
    expect(screen.getByTestId('unavailable-2020')).toHaveTextContent('No data')
  })

  it('labels the latest value at the end of the line', () => {
    render(
      <TrendChart
        series={[{ label: 'All', points: points }]}
        unit="minutes_per_day"
        measureLabel="Average time per day"
      />,
    )
    // 2025: 309.6 minutes.
    expect(screen.getByTestId('end-label')).toHaveTextContent('5h 10m')
  })

  it('renders no point at zero for the unavailable year', () => {
    render(
      <TrendChart
        series={[{ label: 'All', points: points }]}
        unit="minutes_per_day"
        measureLabel="Average time per day"
      />,
    )
    expect(screen.queryByTestId('point-2020')).not.toBeInTheDocument()
  })

  it('draws confidence bands for each segment when SEs are present', () => {
    render(
      <TrendChart
        series={[{ label: 'All', points: points }]}
        unit="minutes_per_day"
        measureLabel="Average time per day"
      />,
    )
    expect(screen.getAllByTestId('ci-band').length).toBe(2)
  })
})

describe('TrendChart keyboard access', () => {
  it('walks years with arrow keys and announces values including the gap', async () => {
    const user = userEvent.setup()
    render(
      <TrendChart
        series={[{ label: 'All', points: points }]}
        unit="minutes_per_day"
        measureLabel="Average time per day"
      />,
    )
    const chart = screen.getByRole('application', { name: /Average time per day by year/ })
    chart.focus()
    await user.keyboard('{ArrowRight}')
    // 2003: 306.6 minutes, read as a duration.
    const announced = chart.querySelector('[aria-live]') as HTMLElement
    expect(announced).toHaveTextContent('2003: 5h 7m')
    await user.keyboard('{End}{ArrowLeft}{ArrowLeft}{ArrowLeft}{ArrowLeft}{ArrowLeft}')
    // 2025 → five steps left = 2020, the unavailable year.
    expect(announced).toHaveTextContent(/2020: no estimate/i)
    expect(chart.querySelector('.chart-tooltip')).toHaveTextContent(
      'No data: the survey paused in spring 2020 (COVID-19).',
    )
  })
})

describe('TrendChart readout', () => {
  it('says the value and the year only — the band carries the uncertainty', async () => {
    const user = userEvent.setup()
    render(
      <TrendChart
        series={[{ label: 'All', points: points }]}
        unit="minutes_per_day"
        measureLabel="Average time per day"
      />,
    )
    const chart = screen.getByRole('application', { name: /Average time per day by year/ })
    chart.focus()
    await user.keyboard('{ArrowRight}')
    const tooltip = chart.querySelector('.chart-tooltip') as HTMLElement
    expect(tooltip).toHaveTextContent(/^5h 7m2003$/)
    expect(chart).not.toHaveTextContent(/standard error|CI/i)
  })

  it('labels a duration axis with clock-like ticks, in the page’s duration form', () => {
    render(
      <TrendChart
        series={[{ label: 'All', points: points }]}
        unit="minutes_per_day"
        measureLabel="Average time per day"
      />,
    )
    const ticks = Array.from(
      document.querySelectorAll('.chart-tick--y'),
      (tick) => tick.textContent,
    )
    expect(ticks.length).toBeGreaterThanOrEqual(2)
    for (const tick of ticks) expect(tick).toMatch(/^\d+h( [1-5]?\dm)?$/)
    expect(ticks.some((tick) => / 0\dm$/.test(tick ?? ''))).toBe(false)
  })
})

describe('degenerate trends', () => {
  it('explains when no year could be estimated', () => {
    const unavailable: TrendPoint[] = [
      { year: 2020, estimate: null, unavailable_reason: 'TUFNWGTP is undefined for 2020.' },
    ]
    render(
      <TrendChart
        series={[{ label: 'All', points: unavailable }]}
        unit="minutes_per_day"
        measureLabel="Average"
      />,
    )
    expect(screen.getByText(/None of the requested years/)).toBeInTheDocument()
  })
})

describe('TrendChart partial years and split lines (review round 4)', () => {
  /** The leisure trend with 2020 filled in as a partial-year point. */
  const withPartial: TrendPoint[] = points.map((point) =>
    point.year === 2020
      ? {
          ...point,
          estimate: { value: 330, unit: 'minutes_per_day', confidence_level: 0.95 },
          unavailable_reason: null,
        }
      : point,
  )

  it('reaches a partial-year point by a dotted connector, never by the line itself', () => {
    render(
      <TrendChart
        series={[{ label: 'All', points: withPartial, partialYears: [2020] }]}
        unit="minutes_per_day"
        measureLabel="Average time per day"
      />,
    )
    const partialX = /cx="([\d.]+)"/.exec(screen.getByTestId('point-2020').outerHTML)?.[1]
    const connectors = screen.getAllByTestId('trend-line-connector')
    expect(connectors.length).toBe(1)
    expect(connectors[0]?.getAttribute('class')).toContain('chart-line--partial')
    expect(connectors[0]?.getAttribute('points')).toContain(`${partialX},`)
    for (const segment of screen.getAllByTestId('trend-line-segment')) {
      expect(segment.getAttribute('points')).not.toContain(`${partialX},`)
    }
  })

  it('draws a split trend’s points only at its lines’ ends, and labels each end', () => {
    const shifted = points.map((point) =>
      point.estimate == null
        ? point
        : { ...point, estimate: { ...point.estimate, value: point.estimate.value - 60 } },
    )
    const { container } = render(
      <TrendChart
        series={[
          { label: 'Men', points },
          { label: 'Women', points: shifted },
        ]}
        unit="minutes_per_day"
        measureLabel="Average time per day"
        legendTitle="Sex"
      />,
    )
    expect(screen.getByTestId('series-0').querySelectorAll('circle').length).toBe(1)
    const labels = screen.getAllByTestId('series-end-label').map((label) => label.textContent)
    expect(labels).toEqual(['Men 5h 10m', 'Women 4h 10m'])
    // The key comes before the chart it explains.
    const legend = container.querySelector('.chart-legend') as Element
    const chart = container.querySelector('.chart-wrap') as Element
    expect(
      legend.compareDocumentPosition(chart) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })
})
