/**
 * Friendly aliases over the generated OpenAPI types.
 *
 * `generated.ts` is produced from the backend's own OpenAPI schema
 * (`npm run generate:api-types`, see docs/frontend.md) — these aliases are the
 * only names the rest of the frontend should import, so the app's vocabulary
 * stays 1:1 with the actual Phase 3 contract.
 */
import type { components } from './generated'

type Schemas = components['schemas']

// Requests
export type ActivitySelection = Schemas['ActivitySelection']
export type PopulationRequest = Schemas['PopulationRequest']
export type EstimateRequest = Schemas['EstimateRequest']
export type TrendRequest = Schemas['TrendRequest']
export type CompareRequest = Schemas['CompareRequest']

// Responses
export type EstimateValue = Schemas['EstimateValueModel']
export type ActivityInfo = Schemas['ActivityInfoModel']
export type WeightInfo = Schemas['WeightInfoModel']
export type EstimateResponse = Schemas['EstimateResponse']
export type TrendPoint = Schemas['TrendPointModel']
export type TrendResponse = Schemas['TrendResponse']
export type CompareResponse = Schemas['CompareResponse']

// Discovery / metadata
export type ActivityEntry = Schemas['ActivityEntry']
export type ActivityListResponse = Schemas['ActivityListResponse']
export type ActivityDetailResponse = Schemas['ActivityDetailResponse']
export type ActivityPreset = Schemas['ActivityPreset']
export type ActivityPresetsResponse = Schemas['ActivityPresetsResponse']
export type PopulationDimension = Schemas['PopulationDimension']
export type PopulationMetadataResponse = Schemas['PopulationMetadataResponse']
export type MeasureInfo = Schemas['MeasureInfo']
export type WeightSchemeInfo = Schemas['WeightSchemeInfo']
export type CapabilitiesInfo = Schemas['CapabilitiesInfo']
export type DataInfo = Schemas['DataInfo']
export type MetaResponse = Schemas['MetaResponse']

// Errors
export type ErrorDetail = Schemas['ErrorDetail']
export type ErrorResponse = Schemas['ErrorResponse']

/** The three analysis operations, matching POST /api/v1/analysis/{operation}. */
export type Operation = 'estimate' | 'trend' | 'compare'

export type AnalysisRequest = EstimateRequest | TrendRequest | CompareRequest
export type AnalysisResponse = EstimateResponse | TrendResponse | CompareResponse

/** Maps each operation to its request/response pair. */
export interface OperationContract {
  estimate: { request: EstimateRequest; response: EstimateResponse }
  trend: { request: TrendRequest; response: TrendResponse }
  compare: { request: CompareRequest; response: CompareResponse }
}

export const OPERATIONS: readonly Operation[] = ['estimate', 'trend', 'compare']

export function isOperation(value: string | undefined): value is Operation {
  return value === 'estimate' || value === 'trend' || value === 'compare'
}
