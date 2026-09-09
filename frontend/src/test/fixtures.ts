/**
 * Test fixtures: real responses captured from the running Phase 3 API
 * against the loaded 2003–2025 database (September 2026). Never hand-edit
 * the JSON — recapture from the API so tests always reflect the actual
 * contract. Importing them through the API types makes `tsc` verify that
 * the captured payloads satisfy the frontend's type definitions.
 */
import type {
  ActivityDetailResponse,
  ActivityListResponse,
  ActivityPresetsResponse,
  CompareResponse,
  ErrorResponse,
  EstimateResponse,
  MetaResponse,
  PopulationMetadataResponse,
  TrendResponse,
} from '../api/types'

import activitiesJson from './fixtures/activities.json'
import activityDetailJson from './fixtures/activity_0101.json'
import presetsJson from './fixtures/activity_presets.json'
import compareChildrenJson from './fixtures/compare_children.json'
import error2020Json from './fixtures/error_2020_multiyear.json'
import errorInvalidSpecJson from './fixtures/error_invalid_spec.json'
import errorUnknownActivityJson from './fixtures/error_unknown_activity.json'
import estimatePandemicJson from './fixtures/estimate_pandemic.json'
import estimateSleepJson from './fixtures/estimate_sleep.json'
import estimateTvJson from './fixtures/estimate_tv_participation.json'
import metaJson from './fixtures/meta.json'
import populationMetadataJson from './fixtures/population_metadata.json'
import trendLeisureJson from './fixtures/trend_leisure.json'

export const metaFixture = metaJson as MetaResponse
export const populationMetadataFixture = populationMetadataJson as PopulationMetadataResponse
export const activitiesFixture = activitiesJson as ActivityListResponse
export const presetsFixture = presetsJson as ActivityPresetsResponse
export const activityDetailFixture = activityDetailJson as ActivityDetailResponse

/** Sleep, ages 25–54, 2025: 528.754… min/day, SE 2.938…, n=2,597. */
export const estimateSleepFixture = estimateSleepJson as EstimateResponse
/** TV participation rate 2024: 0.7278…, unit proportion_of_population. */
export const estimateTvFixture = estimateTvJson as EstimateResponse
/** Sleep 2020 under pandemic weights: carries the collection-window warning. */
export const estimatePandemicFixture = estimatePandemicJson as EstimateResponse
/** Leisure 2003–2025: 22 available points, 2020 unavailable with reason. */
export const trendLeisureFixture = trendLeisureJson as TrendResponse
/** Household activities 2025, children vs not: difference −11.09… ± 4.24. */
export const compareChildrenFixture = compareChildrenJson as CompareResponse

export const error2020Fixture = error2020Json as ErrorResponse
export const errorUnknownActivityFixture = errorUnknownActivityJson as ErrorResponse
export const errorInvalidSpecFixture = errorInvalidSpecJson as ErrorResponse
