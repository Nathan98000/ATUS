import { describe, expect, it } from 'vitest'

import { durationTicks, durationTicksFromZero, ticksFromZero } from './chartScale'

describe('ticks from zero', () => {
  it('reaches at least the maximum on round steps', () => {
    expect(ticksFromZero(0.45)).toEqual([0, 0.2, 0.4, 0.6])
    expect(ticksFromZero(176)).toEqual([0, 50, 100, 150, 200])
  })

  it('steps durations like a clock', () => {
    expect(durationTicksFromZero(176)).toEqual([0, 60, 120, 180])
    expect(durationTicksFromZero(546)).toEqual([0, 120, 240, 360, 480, 600])
    expect(durationTicksFromZero(42)).toEqual([0, 10, 20, 30, 40, 50])
  })
})

describe('duration ticks inside a range', () => {
  it('steps like a clock without starting at zero', () => {
    expect(durationTicks(298, 328)).toEqual([300, 310, 320])
    // Ten-minute steps would make seven ticks: quarter hours instead.
    expect(durationTicks(500, 560)).toEqual([510, 525, 540, 555])
  })

  it('falls back to round decimals for a range under a minute', () => {
    expect(durationTicks(10.2, 10.6)).toEqual([10.2, 10.3, 10.4, 10.5, 10.6])
  })
})
