/**
 * A one-year figure that rests on few diaries offers the same analysis over
 * the latest years combined (review round 6, the owner's decision D7): the
 * remedy the builder no longer offers as a control. The years are the
 * latest three up to the figure's own, without 2020 — the standard weights
 * have none, and 2020 combines only with 2019 — or 2019 with 2020 for a 2020
 * figure. The link is written under the site's settings
 * (`withAnalysisSettings`), as the builder would write it.
 */
import type { AnalysisRequest, Operation } from '../../api/types'
import { analysisPath } from '../../domain/urlSpec'
import { formatYears } from '../../utils/format'
import { PARTIAL_YEAR, withAnalysisSettings } from '../builder/state'

/** How many years the link combines. */
const COMBINED_YEARS = 3

export function combineYearsLink(
  operation: Operation,
  spec: AnalysisRequest,
  availableYears: readonly number[],
): { path: string; years: string } | null {
  if (operation === 'trend' || spec.years.length !== 1) return null
  const year = spec.years[0] as number
  const ascending = [...availableYears].sort((a, b) => a - b)
  const years =
    year === PARTIAL_YEAR
      ? ascending.filter((candidate) => candidate === PARTIAL_YEAR - 1 || candidate === year)
      : ascending
          .filter((candidate) => candidate <= year && candidate !== PARTIAL_YEAR)
          .slice(-COMBINED_YEARS)
  if (years.length < 2) return null
  const request = withAnalysisSettings(operation, { ...spec, years })
  return { path: analysisPath(operation, request), years: formatYears(years) }
}
