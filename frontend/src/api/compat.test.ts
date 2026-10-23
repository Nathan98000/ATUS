/** Audit F25: the API version is checked, and an unreadable /meta is never guessed at. */
import { describe, expect, it } from 'vitest'

import { metaFixture } from '../test/fixtures'
import {
  DataNotLoadedError,
  IncompatibleApiError,
  apiMajorVersion,
  checkMetaCompatibility,
} from './compat'

describe('apiMajorVersion', () => {
  it.each([
    ['v1', 'v1'],
    ['v1.3', 'v1'],
    ['v01', 'v1'],
    ['v2', 'v2'],
    ['1', null],
    ['version one', null],
    [1, null],
    [undefined, null],
  ])('%s → %s', (input, expected) => {
    expect(apiMajorVersion(input)).toBe(expected)
  })
})

describe('checkMetaCompatibility', () => {
  it('accepts the captured v1 /meta, and additive v1 changes', () => {
    expect(checkMetaCompatibility(metaFixture)).toBe(metaFixture)
    const additive = { ...metaFixture, api_version: 'v1.1', new_field: { anything: true } }
    expect(checkMetaCompatibility(additive)).toBe(additive)
  })

  it('rejects another major version, naming it', () => {
    const failure = (() => {
      try {
        checkMetaCompatibility({ ...metaFixture, api_version: 'v2' })
      } catch (error) {
        return error
      }
    })() as IncompatibleApiError
    expect(failure).toBeInstanceOf(IncompatibleApiError)
    expect(failure.reportedVersion).toBe('v2')
  })

  it.each([
    ['not an object', 'v1 please'],
    ['no version', { ...metaFixture, api_version: undefined }],
    ['years not integers', { ...metaFixture, data: { ...metaFixture.data, years: ['2025'] } }],
    ['no data block', { ...metaFixture, data: null }],
    [
      'measures renamed',
      {
        ...metaFixture,
        capabilities: { ...metaFixture.capabilities, measures: undefined, statistics: [] },
      },
    ],
  ])('rejects an unreadable /meta (%s)', (_label, value) => {
    expect(() => checkMetaCompatibility(value)).toThrow(IncompatibleApiError)
  })
})

function thrown(value: unknown): unknown {
  try {
    checkMetaCompatibility(value)
  } catch (error) {
    return error
  }
  return null
}

describe('checkMetaCompatibility: a service with no data loaded (review round 2)', () => {
  it.each([
    [
      'the API’s own /meta before any load',
      {
        ...metaFixture,
        data: { ...metaFixture.data, release: 'unloaded', years: [], ingestion_run_id: null },
      },
    ],
    ['no years', { ...metaFixture, data: { ...metaFixture.data, years: [] } }],
  ])('is data not loaded, not an incompatible API (%s)', (_label, value) => {
    const failure = thrown(value)
    expect(failure).not.toBeInstanceOf(IncompatibleApiError)
    expect(failure).toBeInstanceOf(DataNotLoadedError)
  })

  it('another major version with no data is still incompatible', () => {
    const value = {
      ...metaFixture,
      api_version: 'v2',
      data: { release: 'unloaded', years: [] },
    }
    expect(thrown(value)).toBeInstanceOf(IncompatibleApiError)
  })
})
