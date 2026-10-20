/**
 * The result page's downloads, end to end in the component tree: the file a
 * click produces holds exactly the API response on screen (MSW fixtures),
 * through a Blob URL (CSP-compatible), with the page's analysis key, data
 * version and canonical link.
 */
import { QueryClientProvider } from '@tanstack/react-query'
import userEvent from '@testing-library/user-event'
import { render, screen } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { EstimateRequest, TrendRequest } from '../../api/types'
import { estimateCsv, trendCsv } from '../../domain/exports'
import { encodeSpec } from '../../domain/urlSpec'
import { AnalysisPage } from '../../pages/AnalysisPage'
import { estimateSleepFixture, metaFixture, trendLeisureFixture } from '../../test/fixtures'
import { testQueryClient } from '../../test/render'
import { analysisHeaders, api, server } from '../../test/server'

const sleepSpec: EstimateRequest = {
  activity: { preset: 'sleep' },
  years: [2025],
  population: { age_min: 25, age_max: 54 },
}

interface Saved {
  fileName: string
  type: string
  text: string
}

let saved: Saved[] = []
let blobs: Map<string, Blob>
let revoked: string[] = []

beforeEach(() => {
  saved = []
  revoked = []
  blobs = new Map()
  let counter = 0
  // jsdom has no object URLs; record what the page hands the browser.
  URL.createObjectURL = (blob: Blob) => {
    counter += 1
    const url = `blob:http://localhost/${counter}`
    blobs.set(url, blob)
    return url
  }
  URL.revokeObjectURL = (url: string) => {
    revoked.push(url)
  }
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    const blob = blobs.get(this.href)
    if (!blob || !this.isConnected)
      throw new Error('clicked a link that is not a live Blob URL')
    // Decode the file's bytes as written (Blob.text() would drop a byte-order mark).
    void blob.arrayBuffer().then((bytes) =>
      saved.push({
        fileName: this.download,
        type: blob.type,
        text: new TextDecoder('utf-8', { ignoreBOM: true }).decode(bytes),
      }),
    )
  })
})

afterEach(() => {
  Reflect.deleteProperty(URL, 'createObjectURL')
  Reflect.deleteProperty(URL, 'revokeObjectURL')
})

function renderResult(route: string) {
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path="/analysis/:operation" element={<AnalysisPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

async function waitForSaved(count: number): Promise<Saved[]> {
  await vi.waitFor(() => expect(saved).toHaveLength(count))
  return saved
}

describe('result downloads', () => {
  it('saves the estimate table as CSV, built from the response on screen', async () => {
    const user = userEvent.setup()
    renderResult(`/analysis/estimate?spec=${encodeSpec(sleepSpec)}`)
    await screen.findByText('8h 49m')

    await user.click(screen.getByRole('button', { name: 'Download CSV' }))
    const [file] = await waitForSaved(1)
    expect(file?.fileName).toBe('atus-estimate-sleeping-2025.csv')
    expect(file?.type).toBe('text/csv;charset=utf-8')
    // The page's own data version: this API reports none, so the legacy release:run tag.
    const dataVersion = `${metaFixture.data.release}:run${metaFixture.data.ingestion_run_id}`
    expect(file?.text).toBe(
      estimateCsv(estimateSleepFixture, {
        operation: 'estimate',
        requestSpec: sleepSpec,
        analysisKey: 'a'.repeat(64),
        dataVersion,
        apiVersion: 'v1',
        dataRelease: '0325',
        link: '',
        exportedAt: new Date(),
      }),
    )
    expect(file?.text).toContain(',528.7543524721476,2.938283489022278,')
    // Announced politely; focus stays on the button.
    expect(screen.getByText('Download started: atus-estimate-sleeping-2025.csv')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Download CSV' })).toHaveFocus()
    // The temporary link is gone again.
    expect(document.querySelector('a[download]')).toBeNull()
  })

  it('saves the JSON record with the analysis key, versions and canonical link', async () => {
    const user = userEvent.setup()
    server.use(
      http.get(api('/meta'), () =>
        HttpResponse.json(metaFixture, { headers: { 'X-Data-Version': 'release-a' } }),
      ),
      http.post(api('/analysis/estimate'), () =>
        HttpResponse.json(estimateSleepFixture, {
          headers: { ...analysisHeaders(), 'X-Data-Version': 'release-a' },
        }),
      ),
    )
    renderResult(`/analysis/estimate?spec=${encodeSpec(sleepSpec)}`)
    await screen.findByText('8h 49m')

    await user.click(screen.getByRole('button', { name: 'Download JSON' }))
    const [file] = await waitForSaved(1)
    expect(file?.fileName).toBe('atus-estimate-sleeping-2025.json')
    expect(file?.type).toBe('application/json')
    const document = JSON.parse(file?.text ?? '')
    expect(document).toMatchObject({
      operation: 'estimate',
      analysis_key: 'a'.repeat(64),
      data_version: 'release-a',
      api_version: 'v1',
      analytics_version: '0.2',
      data_release: '0325',
      spec: estimateSleepFixture.spec,
      spec_source: 'api_canonical',
      warnings: estimateSleepFixture.warnings,
    })
    expect(document.response).toEqual(estimateSleepFixture)
    // The link is the page's canonical one (rewritten from the response's spec).
    expect(document.link).toBe(
      `${window.location.origin}/analysis/estimate?spec=${encodeSpec(estimateSleepFixture.spec as EstimateRequest)}`,
    )
  })

  it('writes NA for the unavailable 2020 point of a trend', async () => {
    const user = userEvent.setup()
    const trendSpec: TrendRequest = {
      activity: { preset: 'leisure_and_sports_bls_table' },
      years: trendLeisureFixture.points.map((point) => point.year),
    }
    renderResult(`/analysis/trend?spec=${encodeSpec(trendSpec)}`)
    await screen.findByText(/yearly estimates/)

    await user.click(screen.getByRole('button', { name: 'Download CSV' }))
    const [file] = await waitForSaved(1)
    expect(file?.fileName).toBe(
      'atus-trend-leisure-and-sports-bls-table-a-1-definition-2003-2025.csv',
    )
    const row2020 = file?.text.split('\r\n').find((line) => line.startsWith('2020,'))
    expect(row2020).toContain(',minutes_per_day,NA,NA,NA,NA,NA,NA,NA,NA,TUFNWGTP is undefined')
    expect(file?.text).toBe(
      trendCsv(trendLeisureFixture, {
        operation: 'trend',
        requestSpec: trendSpec,
        analysisKey: 'a'.repeat(64),
        dataVersion: `${metaFixture.data.release}:run${metaFixture.data.ingestion_run_id}`,
        apiVersion: 'v1',
        dataRelease: '0325',
        link: '',
        exportedAt: new Date(),
      }),
    )
  })

  it('releases each object URL after the download has started', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
      renderResult(`/analysis/estimate?spec=${encodeSpec(sleepSpec)}`)
      await screen.findByText('8h 49m')
      await user.click(screen.getByRole('button', { name: 'Download CSV' }))
      expect(revoked).toEqual([])
      await vi.advanceTimersByTimeAsync(10_000)
      expect(revoked).toEqual(['blob:http://localhost/1'])
    } finally {
      vi.useRealTimers()
    }
  })

  it('is not offered while there is no result', async () => {
    server.use(
      http.post(api('/analysis/estimate'), () =>
        HttpResponse.json(
          { error: { code: 'invalid_spec', message: 'Bad.', details: null } },
          { status: 400 },
        ),
      ),
    )
    renderResult(`/analysis/estimate?spec=${encodeSpec(sleepSpec)}`)
    await screen.findByRole('alert')
    expect(screen.queryByRole('button', { name: 'Download CSV' })).toBeNull()
  })
})
