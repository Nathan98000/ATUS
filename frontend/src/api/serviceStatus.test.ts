/** When the waking notice shows (F11) and when a data-version change is detected (F27). */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  ORIGIN_WAKING_TTL_MS,
  SLOW_METADATA_MS,
  isServiceWaking,
  noteServedDataVersion,
  setLoadedDataVersion,
  setOriginState,
  trackApiCall,
  useChangedDataVersion,
} from './serviceStatus'
import { act, renderHook } from '@testing-library/react'

describe('waking', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('a metadata call becomes "waking" after ~3 s without an answer', () => {
    const call = trackApiCall('metadata')
    vi.advanceTimersByTime(SLOW_METADATA_MS - 1)
    expect(isServiceWaking()).toBe(false)
    vi.advanceTimersByTime(1)
    expect(isServiceWaking()).toBe(true)
    call.finish()
    expect(isServiceWaking()).toBe(false)
  })

  it('a long analysis is calculating, not waking', () => {
    const call = trackApiCall('analysis')
    vi.advanceTimersByTime(30_000)
    expect(isServiceWaking()).toBe(false)
    call.finish()
  })

  it('a waking answer shows the notice at once, for any call', () => {
    const call = trackApiCall('analysis')
    call.setWaking(true)
    expect(isServiceWaking()).toBe(true)
    call.answered()
    expect(isServiceWaking()).toBe(false)
    call.finish()
  })

  it('the prewake answer "waking" applies to pending calls until the API answers', () => {
    setOriginState('waking')
    expect(isServiceWaking()).toBe(false) // nothing is waiting on the service
    const call = trackApiCall('analysis')
    expect(isServiceWaking()).toBe(true)
    call.answered()
    expect(isServiceWaking()).toBe(false)
    call.finish()
  })

  it('the prewake answer expires', () => {
    setOriginState('waking')
    const call = trackApiCall('analysis')
    expect(isServiceWaking()).toBe(true)
    vi.advanceTimersByTime(ORIGIN_WAKING_TTL_MS + 1)
    expect(isServiceWaking()).toBe(false)
    call.finish()
  })

  it('"warm" clears it', () => {
    setOriginState('waking')
    const call = trackApiCall('analysis')
    setOriginState('warm')
    expect(isServiceWaking()).toBe(false)
    call.finish()
  })
})

describe('data version', () => {
  it('reports a served version that differs from the loaded one', () => {
    const { result } = renderHook(() => useChangedDataVersion())
    act(() => setLoadedDataVersion('0325:serving-a:111', true))
    act(() => noteServedDataVersion('0325:serving-a:111'))
    expect(result.current).toBeNull()
    act(() => noteServedDataVersion(null))
    expect(result.current).toBeNull()
    act(() => noteServedDataVersion('0325:serving-b:222'))
    expect(result.current).toBe('0325:serving-b:222')
    // Reloading metadata from the new release clears it.
    act(() => setLoadedDataVersion('0325:serving-b:222', true))
    expect(result.current).toBeNull()
  })

  it('compares nothing against the legacy release:run tag', () => {
    const { result } = renderHook(() => useChangedDataVersion())
    act(() => setLoadedDataVersion('0325:run1', false))
    act(() => noteServedDataVersion('0325:serving-b:222'))
    expect(result.current).toBeNull()
  })

  it('ignores responses that arrive before metadata is loaded', () => {
    const { result } = renderHook(() => useChangedDataVersion())
    act(() => noteServedDataVersion('0325:serving-b:222'))
    expect(result.current).toBeNull()
  })
})
