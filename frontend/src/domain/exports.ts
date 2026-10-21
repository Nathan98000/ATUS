/**
 * Result exports: a CSV of the result table and a JSON record of the whole
 * analysis (brief §14, deferred from Phase 4).
 *
 * Nothing here computes a statistic. Every number in a CSV cell is the API's
 * own value, written with JavaScript's shortest round-trip form (the same
 * digits JSON uses), never rounded or derived; the JSON file embeds the API
 * response itself. Unavailable values (a 2020 trend point under the
 * multi-year weights, a standard error that was not requested, a sample size
 * a difference does not have) are the literal `NA`, never zero or blank.
 *
 * CSV conventions (RFC 4180): one header row, one row per estimate (per
 * year for a trend; group A, group B and their difference for a
 * comparison), comma separators, CRLF line endings, fields quoted only when
 * they contain a comma, quote or line break (quotes doubled). The file
 * starts with a UTF-8 byte-order mark so spreadsheet programs read
 * non-ASCII labels correctly. Every row carries its unit and its
 * provenance (weights, versions, warnings, BLS attribution), so a row stays
 * interpretable on its own. A text cell that a spreadsheet would run as a
 * formula (leading `=`, `+`, `-`, `@`, tab or carriage return — a group label
 * from a shared link is untrusted text) is prefixed with an apostrophe, and
 * so is such text after a `;`, tab or line break inside a cell, where
 * spreadsheet programs in `;` locales start a new cell or row.
 */
import type {
  AnalysisRequest,
  AnalysisResponse,
  CompareResponse,
  EstimateResponse,
  EstimateValue,
  Operation,
  TrendResponse,
} from '../api/types'

/** BLS attribution, carried by every export. */
export const BLS_ATTRIBUTION =
  'U.S. Bureau of Labor Statistics, American Time Use Survey (ATUS)'
export const BLS_SURVEY_URL = 'https://www.bls.gov/tus/'
export const INDEPENDENCE_NOTICE =
  'Computed with ATUS Explorer, an independent open-source tool; not an official BLS product and not endorsed by BLS.'
export const PROJECT_URL = 'https://github.com/Nathan98000/ATUS'

/** The spelling of a value the API did not (or could not) provide. */
export const NOT_AVAILABLE = 'NA'

const UTF8_BOM = '﻿'
const CRLF = '\r\n'
/** Separator for list-valued cells (years, activity codes, warnings). */
const LIST_SEPARATOR = ';'
const WARNING_SEPARATOR = ' | '

/** Everything an export needs besides the API response itself. */
export interface ExportContext {
  operation: Operation
  /** The specification this page sent (decoded from its link). */
  requestSpec: AnalysisRequest
  /** X-Analysis-Key of the response (or the snapshot's), when known. */
  analysisKey: string | null
  /**
   * The data release that produced the response: its `X-Data-Version` (or
   * the static snapshot's), else the version the page's metadata reported.
   */
  dataVersion: string | null
  /** `/meta` facts of the service the page talked to. */
  apiVersion: string | null
  /**
   * The release the page's `/meta` names. The JSON export uses it only when
   * `dataVersion` does not name one: during a release switch a response can
   * come from a newer release than the page's metadata.
   */
  dataRelease: string | null
  /** The page's own (canonical, shareable) link. */
  link: string
  exportedAt: Date
}

// ---------------------------------------------------------------------------
// CSV cells

/**
 * A ';', tab or line break followed (after any spaces or double quotes) by a
 * character that starts a formula. Spreadsheet programs whose list separator
 * is ';' (Excel in much of Europe) split a .csv there, and to them the quotes
 * of a comma-separated field in the middle of a line are ordinary
 * characters, so the text after it starts a cell of its own: an unguarded
 * label "Sleep;=1+1;" put "=1+1" in a cell, and a line break inside a label
 * in the middle of a line (a trend's activity, a comparison's groups) ends
 * the row there, so "Men\n=1+1;" began the next row with "=1+1"
 * (review round 4).
 */
const SEPARATED_FORMULA = /[;\t\r\n](?=[ "]*[=+\-@\t\r])/g

/** A cell holding API text: NA when absent, guarded against formula injection. */
function text(value: string | null | undefined): Cell {
  if (value === null || value === undefined) return { raw: NOT_AVAILABLE }
  const guarded = value.replace(SEPARATED_FORMULA, "$&'")
  return { raw: /^[=+\-@\t\r]/.test(guarded) ? `'${guarded}` : guarded }
}

/** A cell holding an API number exactly as received: NA when absent. */
function number(value: number | null | undefined): Cell {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return { raw: NOT_AVAILABLE }
  }
  return { raw: String(value) }
}

function list(values: readonly (string | number)[]): Cell {
  return text(values.join(LIST_SEPARATOR))
}

interface Cell {
  raw: string
}

/** RFC 4180 quoting: only fields with a comma, quote or line break are quoted. */
export function csvField(raw: string): string {
  return /[",\r\n]/.test(raw) ? `"${raw.replaceAll('"', '""')}"` : raw
}

function csvDocument(header: readonly string[], rows: readonly (readonly Cell[])[]): string {
  const lines = [
    header.map(csvField).join(','),
    ...rows.map((row) => row.map((cell) => csvField(cell.raw)).join(',')),
  ]
  return UTF8_BOM + lines.join(CRLF) + CRLF
}

// ---------------------------------------------------------------------------
// Shared column groups

const VALUE_COLUMNS = [
  'unit',
  'estimate',
  'standard_error',
  'ci_lower',
  'ci_upper',
  'confidence_level',
] as const

function valueCells(value: EstimateValue | null | undefined, unit: string | null): Cell[] {
  return [
    text(value?.unit ?? unit),
    number(value?.value),
    number(value?.standard_error),
    number(value?.ci_lower),
    number(value?.ci_upper),
    number(value?.confidence_level),
  ]
}

const PROVENANCE_COLUMNS = [
  'weights',
  'weight_variable',
  'variance_method',
  'analytics_version',
  'data_version',
  'warnings',
  'source',
] as const

interface Provenance {
  weight: { scheme: string; bls_variable: string }
  variance_method: string
  analytics_version: string
  warnings?: readonly string[]
}

function provenanceCells(result: Provenance, context: ExportContext): Cell[] {
  return [
    text(result.weight.scheme),
    text(result.weight.bls_variable),
    text(result.variance_method),
    text(result.analytics_version),
    text(context.dataVersion),
    text((result.warnings ?? []).join(WARNING_SEPARATOR)),
    text(BLS_ATTRIBUTION),
  ]
}

const ACTIVITY_COLUMNS = ['activity', 'activity_include', 'activity_exclude'] as const

function activityCells(activity: EstimateResponse['activity']): Cell[] {
  return [text(activity.label), list(activity.include), list(activity.exclude)]
}

// ---------------------------------------------------------------------------
// One CSV per operation

export function estimateCsv(result: EstimateResponse, context: ExportContext): string {
  const header = [
    ...ACTIVITY_COLUMNS,
    'population',
    'years',
    'measure',
    ...VALUE_COLUMNS,
    'n_respondents',
    'n_participants',
    'weighted_population_per_day',
    'days_in_period',
    ...PROVENANCE_COLUMNS,
  ]
  const row = [
    ...activityCells(result.activity),
    text(result.population),
    list(result.years),
    text(result.measure),
    ...valueCells(result.estimate, null),
    number(result.n_respondents),
    number(result.n_participants),
    number(result.weighted_population_per_day),
    number(result.days_in_period),
    ...provenanceCells(result, context),
  ]
  return csvDocument(header, [row])
}

/** The unit every available point of a trend shares; null when no year is available. */
export function trendUnit(result: TrendResponse): string | null {
  return result.points.find((point) => point.estimate != null)?.estimate?.unit ?? null
}

export function trendCsv(result: TrendResponse, context: ExportContext): string {
  const header = [
    'year',
    ...ACTIVITY_COLUMNS,
    'population',
    'measure',
    ...VALUE_COLUMNS,
    'n_respondents',
    'n_participants',
    'weighted_population_per_day',
    'unavailable_reason',
    ...PROVENANCE_COLUMNS,
  ]
  const unit = trendUnit(result)
  const rows = result.points.map((point) => [
    number(point.year),
    ...activityCells(result.activity),
    text(result.population),
    text(result.measure),
    // An unavailable year still names the series' unit: the column is NA,
    // not of an unknown unit.
    ...valueCells(point.estimate, unit),
    number(point.n_respondents),
    number(point.n_participants),
    number(point.weighted_population_per_day),
    text(point.unavailable_reason),
    ...provenanceCells(result, context),
  ])
  return csvDocument(header, rows)
}

export function compareCsv(result: CompareResponse, context: ExportContext): string {
  const header = [
    'series',
    'label',
    'population',
    ...ACTIVITY_COLUMNS,
    'years',
    'measure',
    ...VALUE_COLUMNS,
    'n_respondents',
    'n_participants',
    'weighted_population_per_day',
    'days_in_period',
    ...PROVENANCE_COLUMNS,
  ]
  const { group_a: groupA, group_b: groupB, difference } = result
  // Activity, years, measure and weights are shared by both groups (one
  // CompareRequest); the difference row repeats them from group A.
  const shared = (group: EstimateResponse): Cell[] => [
    ...activityCells(group.activity),
    list(group.years),
    text(group.measure),
  ]
  const provenance = provenanceCells(
    { ...groupA, analytics_version: result.analytics_version, warnings: result.warnings },
    context,
  )
  const groupRow = (series: string, label: string, group: EstimateResponse): Cell[] => [
    text(series),
    text(label),
    text(group.population),
    ...shared(group),
    ...valueCells(group.estimate, null),
    number(group.n_respondents),
    number(group.n_participants),
    number(group.weighted_population_per_day),
    number(group.days_in_period),
    ...provenance,
  ]
  const differenceRow: Cell[] = [
    text('difference'),
    text(`${result.label_a} minus ${result.label_b}`),
    text(null),
    ...shared(groupA),
    ...valueCells(difference, null),
    number(null),
    number(null),
    number(null),
    number(null),
    ...provenance,
  ]
  return csvDocument(header, [
    groupRow('group_a', result.label_a, groupA),
    groupRow('group_b', result.label_b, groupB),
    differenceRow,
  ])
}

export function resultCsv(result: AnalysisResponse, context: ExportContext): string {
  switch (context.operation) {
    case 'estimate':
      return estimateCsv(result as EstimateResponse, context)
    case 'trend':
      return trendCsv(result as TrendResponse, context)
    case 'compare':
      return compareCsv(result as CompareResponse, context)
  }
}

// ---------------------------------------------------------------------------
// JSON

/** Identifies the JSON layout below; bump `ANALYSIS_EXPORT_VERSION` when it changes. */
export const ANALYSIS_EXPORT_FORMAT = 'atus-explorer/analysis-export'
export const ANALYSIS_EXPORT_VERSION = 1

export interface AnalysisExport {
  format: typeof ANALYSIS_EXPORT_FORMAT
  format_version: typeof ANALYSIS_EXPORT_VERSION
  exported_at: string
  operation: Operation
  /** The page's shareable link: opening it reruns this exact analysis. */
  link: string
  /**
   * The API's canonical specification (estimate, trend); a comparison has
   * none, so it is the request this page sent (each group's canonical
   * specification is inside `response`).
   */
  spec: unknown
  spec_source: 'api_canonical' | 'request'
  analysis_key: string | null
  api_version: string | null
  analytics_version: string
  data_version: string | null
  data_release: string | null
  /** The unit of every value in the result (null for a trend with no available year). */
  unit: string | null
  warnings: readonly string[]
  attribution: {
    source: string
    source_url: string
    notice: string
    tool_url: string
  }
  /** The API's response, unchanged: every estimate, SE and CI as computed by the engine. */
  response: AnalysisResponse
}

function resultUnit(result: AnalysisResponse, operation: Operation): string | null {
  switch (operation) {
    case 'estimate':
      return (result as EstimateResponse).estimate.unit
    case 'trend':
      return trendUnit(result as TrendResponse)
    case 'compare':
      return (result as CompareResponse).group_a.estimate.unit
  }
}

export function analysisExport(
  result: AnalysisResponse,
  context: ExportContext,
): AnalysisExport {
  const canonical =
    context.operation === 'compare' ? null : (result as EstimateResponse | TrendResponse).spec
  return {
    format: ANALYSIS_EXPORT_FORMAT,
    format_version: ANALYSIS_EXPORT_VERSION,
    exported_at: context.exportedAt.toISOString(),
    operation: context.operation,
    link: context.link,
    spec: canonical ?? context.requestSpec,
    spec_source: canonical ? 'api_canonical' : 'request',
    analysis_key: context.analysisKey,
    api_version: context.apiVersion,
    analytics_version: result.analytics_version,
    data_version: context.dataVersion,
    data_release: releaseOf(context.dataVersion) ?? context.dataRelease,
    unit: resultUnit(result, context.operation),
    warnings: result.warnings ?? [],
    attribution: {
      source: BLS_ATTRIBUTION,
      source_url: BLS_SURVEY_URL,
      notice: INDEPENDENCE_NOTICE,
      tool_url: PROJECT_URL,
    },
    response: result,
  }
}

/**
 * The release a data version was written for: every format starts with
 * `<release>:` (src/atus_pipeline/database/versions.py); null for an unknown
 * version and for `unloaded:run0`.
 */
function releaseOf(dataVersion: string | null): string | null {
  const release = dataVersion?.split(':', 1)[0] ?? ''
  return release !== '' && release !== 'unloaded' && dataVersion?.includes(':') ? release : null
}

export function resultJson(result: AnalysisResponse, context: ExportContext): string {
  return `${JSON.stringify(analysisExport(result, context), null, 2)}\n`
}

// ---------------------------------------------------------------------------
// File names

/** Lowercase ASCII words joined by hyphens: "Leisure & sports (BLS)" → "leisure-sports-bls". */
export function slugify(value: string, maxLength = 60): string {
  const slug = value
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
  return slug.slice(0, maxLength).replace(/-+$/, '')
}

function yearsSlug(years: readonly number[]): string {
  if (years.length === 0) return ''
  const first = Math.min(...years)
  const last = Math.max(...years)
  return first === last ? String(first) : `${first}-${last}`
}

/** `atus-<operation>-<activity>-<years>.<extension>`, e.g. `atus-estimate-sleeping-2023.csv`. */
export function exportFileName(
  result: AnalysisResponse,
  operation: Operation,
  extension: 'csv' | 'json',
): string {
  let activity: string
  let years: readonly number[]
  if (operation === 'trend') {
    const trend = result as TrendResponse
    activity = trend.activity.label
    years = trend.points.map((point) => point.year)
  } else {
    const estimate =
      operation === 'compare'
        ? (result as CompareResponse).group_a
        : (result as EstimateResponse)
    activity = estimate.activity.label
    years = estimate.years
  }
  const parts = ['atus', operation, slugify(activity), yearsSlug(years)].filter(
    (part) => part !== '',
  )
  return `${parts.join('-')}.${extension}`
}
