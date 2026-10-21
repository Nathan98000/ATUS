/**
 * "Download this result": the result table as CSV and the whole analysis as
 * JSON (specification, versions, warnings, attribution and the API's
 * response). Both are built in the browser from the response already on
 * screen (`domain/exports.ts`) — no request, no recomputed statistic.
 */
import { useEffect, useRef, useState } from 'react'
import { useLocation } from 'react-router'

import type { AnalysisRequest, AnalysisResponse, Operation } from '../../api/types'
import { type ExportContext, exportFileName, resultCsv, resultJson } from '../../domain/exports'
import { downloadText } from '../../utils/download'

/**
 * A repeated download of the same file sets the same status text, which
 * changes nothing in the DOM, so the live region would stay silent: it is
 * emptied first and given the text again after this delay (review round 2).
 */
const ANNOUNCE_AGAIN_MS = 100

const ROW_DESCRIPTION: Record<Operation, string> = {
  estimate: 'one row',
  trend: 'one row per year, with NA for a year that has no estimate',
  compare: 'one row per group plus their difference',
}

export function ResultDownloads({
  operation,
  requestSpec,
  result,
  analysisKey,
  dataVersion,
  apiVersion,
  dataRelease,
}: {
  operation: Operation
  requestSpec: AnalysisRequest
  result: AnalysisResponse
  analysisKey: string | null
  dataVersion: string | null
  apiVersion: string | null
  dataRelease: string | null
}) {
  const [status, setStatus] = useState('')
  const announceAgain = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => () => clearTimeout(announceAgain.current), [])
  // The current route, so the link follows the page's rewrite to the canonical spec.
  const location = useLocation()

  const announce = (message: string) => {
    clearTimeout(announceAgain.current)
    if (message !== status) {
      setStatus(message)
      return
    }
    setStatus('')
    announceAgain.current = setTimeout(() => setStatus(message), ANNOUNCE_AGAIN_MS)
  }

  const save = (format: 'csv' | 'json') => {
    const context: ExportContext = {
      operation,
      requestSpec,
      analysisKey,
      dataVersion,
      apiVersion,
      dataRelease,
      link: `${window.location.origin}${location.pathname}${location.search}`,
      exportedAt: new Date(),
    }
    const fileName = exportFileName(result, operation, format)
    if (format === 'csv') {
      downloadText(fileName, resultCsv(result, context), 'text/csv;charset=utf-8')
    } else {
      downloadText(fileName, resultJson(result, context), 'application/json')
    }
    announce(`Download started: ${fileName}`)
  }

  return (
    <section className="card result-downloads" aria-labelledby="result-downloads-heading">
      <h2 id="result-downloads-heading">Download this result</h2>
      <p className="field-hint">
        <b>CSV</b>: the result table ({ROW_DESCRIPTION[operation]}) with units, standard errors
        and confidence intervals. <b>JSON</b>: the exact specification, data and analytics
        versions, warnings, analysis key and BLS attribution, with the complete response of the
        analysis service. Numbers are exactly as the service computed them.
      </p>
      <div className="result-downloads__actions">
        <button type="button" className="btn btn--small" onClick={() => save('csv')}>
          Download CSV
        </button>
        <button type="button" className="btn btn--small" onClick={() => save('json')}>
          Download JSON
        </button>
        <output className="result-downloads__status">{status}</output>
      </div>
    </section>
  )
}
