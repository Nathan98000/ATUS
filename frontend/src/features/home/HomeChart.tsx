/**
 * The home page's chart: household activities, men and women, across every
 * year — a trend split by sex, drawn by the analysis page's own chart, with
 * its one-sentence answer (review round 4: the home page showed no chart).
 * Like the starter previews it is read from the deployed static snapshot
 * only (`useSnapshotAnalysis`), so a home page visit never wakes the
 * analysis service; without a snapshot (development) it is not shown.
 */
import { Link } from 'react-router'

import { useSnapshotAnalysis } from '../../api/queries'
import type { TrendResponse } from '../../api/types'
import { interpretSplitTrend, populationPhrase } from '../../domain/describe'
import { HOME_CHART, homeChartSpecs } from '../../domain/prewarm'
import { analysisPath } from '../../domain/urlSpec'
import { formatYears } from '../../utils/format'
import { withPartial2020 } from '../results/partial2020'
import { TrendChart } from '../results/TrendChart'

export function HomeChart({ years }: { years: readonly number[] }) {
  const specs = homeChartSpecs(years)
  const [men, women] = specs.series
  const menTrend = useSnapshotAnalysis('trend', men?.spec ?? null)
  const womenTrend = useSnapshotAnalysis('trend', women?.spec ?? null)
  const menPartial = useSnapshotAnalysis('estimate', men?.partial2020 ?? null)
  const womenPartial = useSnapshotAnalysis('estimate', women?.partial2020 ?? null)

  const trends = [menTrend.data, womenTrend.data]
  // The 2020 points are optional (a missing one leaves 2020 a gap), but the
  // chart waits for their answer so it is drawn once.
  const partialsPending =
    (men?.partial2020 != null && menPartial.isPending) ||
    (women?.partial2020 != null && womenPartial.isPending)
  if (men === undefined || women === undefined || partialsPending) return null
  if (trends.some((trend) => trend == null)) return null

  const groups = [
    { ...men, result: trends[0] as TrendResponse, partial: menPartial.data ?? undefined },
    { ...women, result: trends[1] as TrendResponse, partial: womenPartial.data ?? undefined },
  ].map((group) => ({
    label: populationPhrase(group.population),
    filters: group.population,
    result: group.result,
    ...withPartial2020(group.result.points, group.partial),
  }))
  const unit =
    groups.flatMap((group) => group.points).find((point) => point.estimate != null)?.estimate
      ?.unit ?? 'minutes_per_day'
  const hasPartial = groups.some((group) => group.partialYears.length > 0)

  return (
    <section className="home-chart card" aria-labelledby="home-chart-heading">
      <h2 id="home-chart-heading" className="home-chart__title">
        Time on household activities, men and women, {formatYears(specs.trend.years)}
      </h2>
      <p className="home-chart__lead">{interpretSplitTrend(groups)}</p>
      <TrendChart
        series={groups}
        unit={unit}
        measureLabel="Average time per day"
        legendTitle="Sex"
      />
      <p className="home-chart__small-print">
        Average time per day. {hasPartial ? '2020 (hollow points) is a partial year. ' : ''}
        Source: American Time Use Survey.{' '}
        <Link
          to={analysisPath('trend', specs.trend, {
            by: HOME_CHART.split.by,
            groups: [...HOME_CHART.split.groups],
          })}
        >
          Open this chart
        </Link>
      </p>
    </section>
  )
}
