import { describe, expect, it } from 'vitest'

import { durationTicksFromZero, ticksFromZero } from './chartScale'

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
