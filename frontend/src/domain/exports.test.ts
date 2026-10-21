/**
 * Result exports: exact CSV text (quoting, units, NA), values identical to
 * the API's, the JSON record, and file names.
 */
import { describe, expect, it } from 'vitest'

import type {
  CompareRequest,
  CompareResponse,
  EstimateResponse,
  TrendResponse,
} from '../api/types'
import {
  compareChildrenFixture,
  estimateSleepFixture,
  trendLeisureFixture,
} from '../test/fixtures'
import {
  ANALYSIS_EXPORT_FORMAT,
  BLS_ATTRIBUTION,
  type ExportContext,
  analysisExport,
  compareCsv,
  csvField,
  estimateCsv,
  exportFileName,
  resultCsv,
  resultJson,
  slugify,
  trendCsv,
} from './exports'

const DATA_VERSION = '0325:serving-20261020T120000Z-0a1b2c3d:00112233aabb'
const SOURCE = '"U.S. Bureau of Labor Statistics, American Time Use Survey (ATUS)"'
const LEXICON_WARNING =
  'Activity codes use the BLS-harmonized 2003-25 multi-year lexicon (cross-year comparable by construction).'

function context(overrides: Partial<ExportContext> = {}): ExportContext {
  return {
    operation: 'estimate',
    requestSpec: { activity: { preset: 'sleep' }, years: [2025] },
    analysisKey: 'a'.repeat(64),
    dataVersion: DATA_VERSION,
    apiVersion: 'v1',
    dataRelease: '0325',
    link: 'https://atus.example.workers.dev/analysis/estimate?spec=abc',
    exportedAt: new Date('2026-10-20T12:00:00Z'),
    ...overrides,
  }
}

/** A minimal RFC 4180 reader, to check that quoted fields round-trip. */
function parseCsv(text: string): string[][] {
  expect(text.startsWith('﻿')).toBe(true)
  const rows: string[][] = []
  let row: string[] = []
  let field = ''
  let quoted = false
  const body = text.slice(1)
  for (let index = 0; index < body.length; index += 1) {
    const char = body[index]
    if (quoted) {
      if (char === '"' && body[index + 1] === '"') {
        field += '"'
        index += 1
      } else if (char === '"') {
        quoted = false
      } else {
        field += char
      }
    } else if (char === '"') {
      quoted = true
    } else if (char === ',') {
      row.push(field)
      field = ''
    } else if (char === '\r' && body[index + 1] === '\n') {
      row.push(field)
      rows.push(row)
      row = []
      field = ''
      index += 1
    } else {
      field += char
    }
  }
  expect(field).toBe('') // the document ends with CRLF
  return rows
}

/** Rows as header → value records. */
function records(text: string): Record<string, string>[] {
  const [header, ...rows] = parseCsv(text)
  return rows.map((row) => {
    expect(row).toHaveLength(header?.length ?? 0)
    return Object.fromEntries((header ?? []).map((name, index) => [name, row[index] ?? '']))
  })
}

describe('csvField', () => {
  it('quotes only fields with a comma, quote or line break, doubling quotes', () => {
    expect(csvField('plain text')).toBe('plain text')
    expect(csvField('a,b')).toBe('"a,b"')
    expect(csvField('say "hi"')).toBe('"say ""hi"""')
    expect(csvField('two\nlines')).toBe('"two\nlines"')
    expect(csvField('cr\rlf')).toBe('"cr\rlf"')
    expect(csvField('')).toBe('')
  })
})

describe('estimate CSV', () => {
  it('is one header row and one row of the API values, exactly', () => {
    expect(estimateCsv(estimateSleepFixture, context())).toBe(
      '﻿' +
        'activity,activity_include,activity_exclude,population,years,measure,unit,estimate,' +
        'standard_error,ci_lower,ci_upper,confidence_level,n_respondents,n_participants,' +
        'weighted_population_per_day,days_in_period,weights,weight_variable,variance_method,' +
        'analytics_version,data_version,warnings,source\r\n' +
        'Sleeping,0101,,age 25-54,2025,average_minutes_per_day,minutes_per_day,' +
        '528.7543524721476,2.938283489022278,522.9954226572953,534.5132822869999,0.95,' +
        '2597,2594,131100441.9822422,365,multiyear,TUFNWGTP,replicate,0.2,' +
        `${DATA_VERSION},${LEXICON_WARNING},${SOURCE}\r\n`,
    )
  })

  it('writes NA, never zero or blank, for uncertainty that was not requested', () => {
    const pointOnly: EstimateResponse = {
      ...estimateSleepFixture,
      variance_method: 'none',
      estimate: {
        ...estimateSleepFixture.estimate,
        standard_error: null,
        ci_lower: null,
        ci_upper: null,
        confidence_level: null,
      },
    }
    const [row] = records(estimateCsv(pointOnly, context()))
    expect(row).toMatchObject({
      estimate: '528.7543524721476',
      standard_error: 'NA',
      ci_lower: 'NA',
      ci_upper: 'NA',
      confidence_level: 'NA',
      variance_method: 'none',
      unit: 'minutes_per_day',
    })
  })

  it('lists pooled years and excluded codes with semicolons', () => {
    const pooled: EstimateResponse = {
      ...estimateSleepFixture,
      years: [2019, 2021, 2022],
      activity: {
        ...estimateSleepFixture.activity,
        include: ['02', '1802'],
        exclude: ['020903'],
      },
    }
    const [row] = records(estimateCsv(pooled, context()))
    expect(row).toMatchObject({
      years: '2019;2021;2022',
      activity_include: '02;1802',
      activity_exclude: '020903',
    })
  })

  it('says NA when the data version is unknown', () => {
    const [row] = records(estimateCsv(estimateSleepFixture, context({ dataVersion: null })))
    expect(row?.data_version).toBe('NA')
  })
})

describe('trend CSV', () => {
  const trend: TrendResponse = {
    ...trendLeisureFixture,
    points: trendLeisureFixture.points.filter((point) =>
      [2019, 2020, 2021].includes(point.year),
    ),
  }

  it('has one row per year; the unavailable 2020 is NA with its reason, never zero', () => {
    const text = trendCsv(trend, context({ operation: 'trend' }))
    const lines = text.slice(1).split('\r\n')
    expect(lines[0]).toBe(
      'year,activity,activity_include,activity_exclude,population,measure,unit,estimate,' +
        'standard_error,ci_lower,ci_upper,confidence_level,n_respondents,n_participants,' +
        'weighted_population_per_day,unavailable_reason,weights,weight_variable,' +
        'variance_method,analytics_version,data_version,warnings,source',
    )
    const point2020 = trend.points.find((point) => point.year === 2020)
    expect(lines[2]).toBe(
      '2020,Leisure and sports (BLS table A-1 definition),12;13;1812;1813,,' +
        'civilian noninstitutional population age 15+,average_minutes_per_day,minutes_per_day,' +
        `NA,NA,NA,NA,NA,NA,NA,NA,${point2020?.unavailable_reason},multiyear,TUFNWGTP,replicate,` +
        `0.2,${DATA_VERSION},${LEXICON_WARNING},${SOURCE}`,
    )
    expect(lines).toHaveLength(5) // header, 3 years, and the empty string after the final CRLF
    expect(lines[4]).toBe('')

    const rows = records(text)
    expect(rows.map((row) => row.year)).toEqual(['2019', '2020', '2021'])
    expect(rows[1]).toMatchObject({
      estimate: 'NA',
      n_respondents: 'NA',
      unit: 'minutes_per_day',
    })
    expect(rows[0]?.unavailable_reason).toBe('NA')
  })

  it('copies every available value exactly as the API sent it', () => {
    const rows = records(trendCsv(trendLeisureFixture, context({ operation: 'trend' })))
    expect(rows).toHaveLength(trendLeisureFixture.points.length)
    trendLeisureFixture.points.forEach((point, index) => {
      const row = rows[index]
      expect(row?.year).toBe(String(point.year))
      if (point.estimate == null) {
        expect(row?.estimate).toBe('NA')
        return
      }
      // Same digits as the API's JSON, so parsing gives back the identical double.
      expect(Number(row?.estimate)).toBe(point.estimate.value)
      expect(Number(row?.standard_error)).toBe(point.estimate.standard_error)
      expect(Number(row?.ci_lower)).toBe(point.estimate.ci_lower)
      expect(Number(row?.ci_upper)).toBe(point.estimate.ci_upper)
      expect(Number(row?.weighted_population_per_day)).toBe(point.weighted_population_per_day)
      expect(row?.estimate).toBe(JSON.stringify(point.estimate.value))
    })
  })

  it('names no unit when no year has an estimate', () => {
    const empty: TrendResponse = {
      ...trend,
      points: trend.points.filter((point) => point.estimate == null),
    }
    const [row] = records(trendCsv(empty, context({ operation: 'trend' })))
    expect(row).toMatchObject({ year: '2020', unit: 'NA', estimate: 'NA' })
  })
})

describe('compare CSV', () => {
  const compare: CompareResponse = {
    ...compareChildrenFixture,
    label_a: 'Parents, "with kids"',
    label_b: '=HYPERLINK("https://evil.example")',
  }

  it('has group A, group B and the API difference, with quoting and NA', () => {
    const text = compareCsv(compare, context({ operation: 'compare' }))
    const lines = text.slice(1).split('\r\n')
    expect(lines[0]).toBe(
      'series,label,population,activity,activity_include,activity_exclude,years,measure,unit,' +
        'estimate,standard_error,ci_lower,ci_upper,confidence_level,n_respondents,' +
        'n_participants,weighted_population_per_day,days_in_period,weights,weight_variable,' +
        'variance_method,analytics_version,data_version,warnings,source',
    )
    expect(lines[1]).toBe(
      'group_a,"Parents, ""with kids""",household children under 18 present,' +
        'Household activities (BLS table A-1 definition),02;1802,020903;020904,2025,' +
        'average_minutes_per_day,minutes_per_day,112.31129417472813,3.313575620672283,' +
        '105.8168052981605,118.80578305129576,0.95,1699,1432,96386457.99280559,365,' +
        'multiyear,TUFNWGTP,replicate,0.2,' +
        `${DATA_VERSION},` +
        // The warnings cell contains a comma, so it is quoted.
        `"${LEXICON_WARNING} | The difference's standard error uses per-replicate ` +
        'differences, which accounts for covariance between the group estimates.",' +
        SOURCE,
    )

    const rows = records(text)
    expect(rows.map((row) => row.series)).toEqual(['group_a', 'group_b', 'difference'])
    expect(rows[0]?.label).toBe('Parents, "with kids"')
    // A label a spreadsheet would run as a formula is neutralized.
    expect(rows[1]?.label).toBe(`'=HYPERLINK("https://evil.example")`)
    expect(rows[2]).toMatchObject({
      label: `Parents, "with kids" minus =HYPERLINK("https://evil.example")`,
      population: 'NA',
      estimate: String(compareChildrenFixture.difference.value),
      standard_error: String(compareChildrenFixture.difference.standard_error),
      ci_lower: String(compareChildrenFixture.difference.ci_lower),
      ci_upper: String(compareChildrenFixture.difference.ci_upper),
      unit: 'minutes_per_day',
      n_respondents: 'NA',
      n_participants: 'NA',
      weighted_population_per_day: 'NA',
      days_in_period: 'NA',
      years: '2025',
    })
    // The difference is the API's (negative) value, written as-is.
    expect(rows[2]?.estimate).toBe('-11.093463784314551')
  })

  it('dispatches by operation', () => {
    expect(resultCsv(compare, context({ operation: 'compare' }))).toBe(
      compareCsv(compare, context({ operation: 'compare' })),
    )
    expect(resultCsv(estimateSleepFixture, context())).toBe(
      estimateCsv(estimateSleepFixture, context()),
    )
  })
})

describe('formula guard where spreadsheets split on ";" (review round 4)', () => {
  // Excel in a ';' list-separator locale splits each CSV line on ';' (and
  // some import dialogs on tabs); the quotes of a comma-separated field in
  // the middle of a line are ordinary characters to it. A label such as
  // "Sleep;=1+1;" was written unguarded, so "=1+1" became a cell of its own.
  const STARTS_FORMULA = /^[=+\-@\t\r]/
  /** The cells such a spreadsheet makes of each line, after the first (spaces and quotes skipped). */
  function separatedCells(text: string): string[] {
    return text
      .slice(1)
      .split('\r\n')
      .flatMap((line) => line.split(/[;\t]/).slice(1))
      .map((cell) => cell.replace(/^[ "]*/, ''))
  }

  it('guards a formula after a ";" in an activity label (the first column)', () => {
    const labelled: EstimateResponse = {
      ...estimateSleepFixture,
      activity: { ...estimateSleepFixture.activity, label: 'Sleep;=1+1;' },
    }
    const text = estimateCsv(labelled, context())
    expect(separatedCells(text).filter((cell) => STARTS_FORMULA.test(cell))).toEqual([])
    // A comma-separated reader sees the label with the guard, as for a leading "=".
    expect(records(text)[0]?.activity).toBe("Sleep;'=1+1;")
  })

  it('guards group labels after ";" or a tab, past spaces and quotes, leaving lists alone', () => {
    const compare: CompareResponse = {
      ...compareChildrenFixture,
      label_a: 'Men;=HYPERLINK("https://evil.example");x',
      label_b: 'Women; -2+3;"=1+1;\t@SUM(A1)',
    }
    const text = compareCsv(compare, context({ operation: 'compare' }))
    expect(separatedCells(text).filter((cell) => STARTS_FORMULA.test(cell))).toEqual([])
    const rows = records(text)
    expect(rows.map((row) => row.label)).toEqual([
      `Men;'=HYPERLINK("https://evil.example");x`,
      `Women;' -2+3;'"=1+1;'\t'@SUM(A1)`,
      `Men;'=HYPERLINK("https://evil.example");x minus Women;' -2+3;'"=1+1;'\t'@SUM(A1)`,
    ])
    // Lists of codes and years are joined with ';' and never start a formula.
    expect(rows[0]).toMatchObject({
      activity_include: '02;1802',
      activity_exclude: '020903;020904',
    })
  })
})

describe('JSON export', () => {
  it('records the canonical spec, versions, warnings, attribution and the untouched response', () => {
    const document = JSON.parse(resultJson(estimateSleepFixture, context()))
    expect(document).toEqual({
      format: ANALYSIS_EXPORT_FORMAT,
      format_version: 1,
      exported_at: '2026-10-20T12:00:00.000Z',
      operation: 'estimate',
      link: 'https://atus.example.workers.dev/analysis/estimate?spec=abc',
      spec: estimateSleepFixture.spec,
      spec_source: 'api_canonical',
      analysis_key: 'a'.repeat(64),
      api_version: 'v1',
      analytics_version: '0.2',
      data_version: DATA_VERSION,
      data_release: '0325',
      unit: 'minutes_per_day',
      warnings: estimateSleepFixture.warnings,
      attribution: {
        source: BLS_ATTRIBUTION,
        source_url: 'https://www.bls.gov/tus/',
        notice: expect.stringContaining('not an official BLS product'),
        tool_url: 'https://github.com/Nathan98000/ATUS',
      },
      response: estimateSleepFixture,
    })
  })

  it('keeps every number of the response identical', () => {
    const text = resultJson(trendLeisureFixture, context({ operation: 'trend' }))
    expect(JSON.parse(text).response).toEqual(trendLeisureFixture)
    expect(text).toContain(JSON.stringify(trendLeisureFixture.points[0]?.estimate?.value))
    expect(text.endsWith('}\n')).toBe(true)
    // 2020 stays null in the JSON, as the API sent it.
    const point2020 = JSON.parse(text).response.points.find(
      (point: { year: number }) => point.year === 2020,
    )
    expect(point2020.estimate).toBeNull()
  })

  it('uses the request for a comparison, which has no single canonical spec', () => {
    const request: CompareRequest = {
      activity: { preset: 'household_activities_bls_table' },
      years: [2025],
      group_a: { has_household_children: true },
      group_b: { has_household_children: false },
    }
    const document = analysisExport(
      compareChildrenFixture,
      context({ operation: 'compare', requestSpec: request }),
    )
    expect(document.spec).toEqual(request)
    expect(document.spec_source).toBe('request')
    expect(document.unit).toBe('minutes_per_day')
    expect(document.warnings).toEqual(compareChildrenFixture.warnings)
  })

  it('names the release of the data version that produced the response (review round 1)', () => {
    // During a release switch the API already serves the new release while
    // the page's /meta (or snapshot) still names the old one: one export
    // must not name two releases.
    const document = analysisExport(
      estimateSleepFixture,
      context({ dataVersion: '0326:serving-20261101T000000Z-0a1b2c3d:aabbccddeeff' }),
    )
    expect(document.data_version).toBe('0326:serving-20261101T000000Z-0a1b2c3d:aabbccddeeff')
    expect(document.data_release).toBe('0326')
    // Every data version format starts with "<release>:" (versions.py).
    for (const [version, release] of [
      ['0325:load-7-20261020T120000Z-abc123', '0325'],
      ['0325:run3:1700000000000000', '0325'],
      ['unloaded:run0', '0325'],
    ]) {
      expect(
        analysisExport(estimateSleepFixture, context({ dataVersion: version })).data_release,
      ).toBe(release)
    }
  })

  it('reports null, not a guess, for facts the page does not know', () => {
    const document = analysisExport(
      estimateSleepFixture,
      context({ analysisKey: null, dataVersion: null, apiVersion: null, dataRelease: null }),
    )
    expect(document).toMatchObject({
      analysis_key: null,
      data_version: null,
      api_version: null,
      data_release: null,
    })
  })
})

describe('file names', () => {
  it('names the operation, the activity and the years', () => {
    expect(exportFileName(estimateSleepFixture, 'estimate', 'csv')).toBe(
      'atus-estimate-sleeping-2025.csv',
    )
    expect(exportFileName(trendLeisureFixture, 'trend', 'json')).toBe(
      'atus-trend-leisure-and-sports-bls-table-a-1-definition-2003-2025.json',
    )
    expect(exportFileName(compareChildrenFixture, 'compare', 'csv')).toBe(
      'atus-compare-household-activities-bls-table-a-1-definition-2025.csv',
    )
  })

  it('slugifies to lowercase ASCII', () => {
    expect(slugify('Café & Crème — “Niño”')).toBe('cafe-creme-nino')
    expect(slugify('***')).toBe('')
    expect(slugify('a'.repeat(80))).toHaveLength(60)
  })
})
