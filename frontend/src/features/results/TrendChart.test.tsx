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
      <TrendChart points={points} unit="minutes_per_day" measureLabel="Average time per day" />,
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

  it('marks the unavailable year with a labeled band and explanation', () => {
    render(
      <TrendChart points={points} unit="minutes_per_day" measureLabel="Average time per day" />,
    )
    expect(screen.getByTestId('unavailable-2020')).toBeInTheDocument()
    expect(screen.getByText(/2020 — no estimate/)).toBeInTheDocument()
    const note = screen.getByTestId('gap-note')
    expect(note).toHaveTextContent('2020 has no estimate')
    expect(note).toHaveTextContent(/TUFNWGTP is undefined for 2020/)
  })

  it('renders no point at zero for the unavailable year', () => {
    render(
      <TrendChart points={points} unit="minutes_per_day" measureLabel="Average time per day" />,
    )
    expect(screen.queryByTestId('point-2020')).not.toBeInTheDocument()
  })

  it('draws confidence bands for each segment when SEs are present', () => {
    render(
      <TrendChart points={points} unit="minutes_per_day" measureLabel="Average time per day" />,
    )
    expect(screen.getAllByTestId('ci-band').length).toBe(2)
  })
})

describe('TrendChart keyboard access', () => {
  it('walks years with arrow keys and announces values including the gap', async () => {
    const user = userEvent.setup()
    render(
      <TrendChart points={points} unit="minutes_per_day" measureLabel="Average time per day" />,
    )
    const chart = screen.getByRole('group', { name: /Average time per day by year/ })
    chart.focus()
    await user.keyboard('{ArrowRight}')
    expect(chart).toHaveTextContent('2003')
    expect(chart).toHaveTextContent('306.6 min')
    await user.keyboard('{End}{ArrowLeft}{ArrowLeft}{ArrowLeft}{ArrowLeft}{ArrowLeft}')
    // 2025 → five steps left = 2020, the unavailable year.
    expect(chart).toHaveTextContent(/2020.*[Uu]navailable/)
  })
})

describe('degenerate trends', () => {
  it('explains when no year could be estimated', () => {
    const unavailable: TrendPoint[] = [
      { year: 2020, estimate: null, unavailable_reason: 'TUFNWGTP is undefined for 2020.' },
    ]
    render(<TrendChart points={unavailable} unit="minutes_per_day" measureLabel="Average" />)
    expect(screen.getByText(/None of the requested years/)).toBeInTheDocument()
  })
})
