/**
 * The prewarmed analyses must be byte-identical to what the UI sends: the
 * home page's starter links and the builder's own output.
 */
import { describe, expect, it } from 'vitest'

import type { AnalysisRequest } from '../api/types'
import {
  activitiesFixture,
  metaFixture,
  populationMetadataFixture,
  presetsFixture,
} from '../test/fixtures'
import {
  defaultBuilderState,
  fromRequest,
  toRequest,
  type BuilderState,
} from '../features/builder/state'
import { resolveSplit, seriesSpec } from '../features/population/compareGroups'
import { partial2020Spec } from '../features/results/partial2020'
import { stableStringify } from '../utils/stableStringify'
import {
  HOME_CHART,
  STARTER_EXAMPLES,
  homeChartSpecs,
  prewarmedAnalyses,
  type PrewarmInput,
} from './prewarm'

const input: PrewarmInput = {
  years: metaFixture.data.years,
  presets: metaFixture.capabilities.activity_presets,
  measures: metaFixture.capabilities.measures.map((measure) => measure.name),
  weightSchemes: metaFixture.capabilities.weight_schemes.map((scheme) => scheme.name),
  tier1: activitiesFixture.activities
    .filter((activity) => activity.level === 1)
    .map((activity) => ({ code: activity.code, name: activity.name })),
}

describe('prewarmedAnalyses', () => {
  const list = prewarmedAnalyses(input)

  it('covers about 130 distinct analyses for the captured release', () => {
    expect(list).toHaveLength(135)
    const identities = new Set(
      list.map((item) => `${item.operation} ${stableStringify(item.spec)}`),
    )
    expect(identities.size).toBe(list.length)
    const counts = (prefix: string) =>
      list.filter((item) => item.name.startsWith(prefix)).length
    expect(counts('starter:')).toBe(4)
    // The household starter: the others equal builder entries above (men vs
    // women is the builder's own default comparison).
    expect(counts('starter via the builder:')).toBe(1)
    expect(counts('estimate:')).toBe(9 * 4 + 18) // presets × measures, tier-1 categories
    expect(counts('trend:')).toBe(9 + 18)
    expect(counts('compare by sex:')).toBe(9 + 18)
    expect(counts('pandemic weights:')).toBe(9 * 2)
    expect(counts('home chart')).toBe(4) // two group trends and their 2020s
  })

  it('holds the home chart in the bytes its split-trend page sends (review round 4)', () => {
    const years = input.years
    const home = homeChartSpecs(years)
    const split = resolveSplit(populationMetadataFixture.dimensions, {
      by: HOME_CHART.split.by,
      groups: [...HOME_CHART.split.groups],
    })
    expect(split).not.toBeNull()
    const preset = presetsFixture.presets.find(
      (candidate) => candidate.name === HOME_CHART.preset,
    )
    home.series.forEach((series, index) => {
      const option = split?.options[index]
      const sent = seriesSpec(home.trend, split as never, option as never)
      expect(stableStringify(series.spec)).toBe(stableStringify(sent))
      expect(list.some((item) => stableStringify(item.spec) === stableStringify(sent))).toBe(
        true,
      )
      // The page asks for 2020 from the trend's canonical spec, whose
      // activity is the preset's codes under its label.
      const canonical = {
        ...sent,
        activity: { include: preset?.include, exclude: preset?.exclude, label: preset?.label },
      } as AnalysisRequest
      expect(stableStringify(series.partial2020)).toBe(
        stableStringify(partial2020Spec(canonical, years, presetsFixture.presets)),
      )
    })
  })

  it('starts with exactly the home page starters', () => {
    const latest = Math.max(...input.years)
    STARTER_EXAMPLES.forEach((starter, index) => {
      const item = list[index]
      expect(item?.operation).toBe(starter.operation)
      expect(stableStringify(item?.spec)).toBe(
        stableStringify(starter.spec(input.years, latest)),
      )
    })
  })

  it('builds every other entry exactly as the analysis builder would', () => {
    const years = input.years
    const builder = (changes: Partial<BuilderState>) =>
      stableStringify(toRequest({ ...defaultBuilderState(years), ...changes }, years))
    const bodyOf = (name: string) => {
      const item = list.find((entry) => entry.name === name)
      expect(item, name).toBeDefined()
      return stableStringify(item?.spec)
    }

    expect(bodyOf('estimate: sleep, participation_rate, 2025')).toBe(
      builder({
        operation: 'estimate',
        activity: { preset: 'sleep' },
        measure: 'participation_rate',
      }),
    )
    expect(bodyOf('trend: 12 Socializing, Relaxing, and Leisure')).toBe(
      builder({
        operation: 'trend',
        activity: { include: ['12'], label: 'Socializing, Relaxing, and Leisure' },
      }),
    )
    expect(bodyOf('compare by sex: watching_tv')).toBe(
      builder({
        operation: 'compare',
        activity: { preset: 'watching_tv' },
        groupA: { sex: 'male' },
        groupB: { sex: 'female' },
      }),
    )
    expect(bodyOf('pandemic weights: sleep, 2020')).toBe(
      builder({
        operation: 'estimate',
        activity: { preset: 'sleep' },
        weights: 'pandemic',
        singleYear: 2020,
      }),
    )
  })

  it('every starter re-run unchanged from the builder is prewarmed too (review round 2)', () => {
    // "Adjust analysis" then Analyze sends the builder's form of a starter
    // (every default stated). For the comparison starters that body was in
    // no snapshot: a cold start and a governor charge for a result the
    // snapshot already held, and a second share link for it.
    const years = input.years
    const latest = Math.max(...years)
    const identities = new Set(
      list.map((item) => `${item.operation} ${stableStringify(item.spec)}`),
    )
    for (const starter of STARTER_EXAMPLES) {
      const viaBuilder = toRequest(
        fromRequest(starter.operation, starter.spec(years, latest), years),
        years,
      )
      expect(
        identities.has(`${starter.operation} ${stableStringify(viaBuilder)}`),
        starter.question,
      ).toBe(true)
    }
  })

  it('a full-period trend has every reported year, 2020 included', () => {
    const trend = list.find((item) => item.name === 'trend: sleep')
    expect(trend?.spec.years).toEqual(input.years)
  })

  it('skips what the release does not offer', () => {
    const smaller = prewarmedAnalyses({
      ...input,
      years: [2023, 2024],
      weightSchemes: ['multiyear'],
      measures: ['average_minutes_per_day'],
    })
    expect(smaller.some((item) => item.name.startsWith('pandemic'))).toBe(false)
    expect(smaller.some((item) => item.name.includes('participation_rate'))).toBe(false)
    expect(prewarmedAnalyses({ ...input, years: [] })).toEqual([])
  })
})
