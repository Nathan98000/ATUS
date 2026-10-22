/**
 * Phase 5 service states, through the whole app (shell + routes) with the
 * API mocked at the HTTP layer: cold start (F11, F12), version mismatch
 * (F25), quota exhaustion and specific errors (F13, F64), background refetch
 * failures (F26), retrying every failed query (F63), data-release changes
 * (F27), and prewake.
 */
import { QueryClientProvider, focusManager, onlineManager } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, delay, http } from 'msw'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { App } from '../App'
import type { EstimateRequest } from '../api/types'
import { WAKING_MESSAGE } from '../components/ServiceNotice'
import { STARTER_EXAMPLES, homeChartSpecs, prewarmedAnalyses } from '../domain/prewarm'
import { encodeSpec } from '../domain/urlSpec'
import {
  activitiesFixture,
  compareChildrenFixture,
  estimatePandemicFixture,
  estimateSleepFixture,
  metaFixture,
  populationMetadataFixture,
  presetsFixture,
  trendLeisureFixture,
} from '../test/fixtures'
import { testQueryClient, withFakeClock } from '../test/render'
import {
  SNAPSHOT_DATA_VERSION,
  SNAPSHOT_ID,
  analysisHeaders,
  api,
  errorResponse,
  installSnapshot,
  server,
} from '../test/server'
import { SNAPSHOT_POINTER_PATH } from '../api/snapshot'
import { stableStringify } from '../utils/stableStringify'

const sleepSpec: EstimateRequest = {
  measure: 'average_minutes_per_day',
  activity: { preset: 'sleep' },
  years: [2025],
  population: { age_min: 25, age_max: 54 },
  weights: 'multiyear',
  variance: 'replicate',
  confidence_level: 0.95,
}
const sleepRoute = `/analysis/estimate?spec=${encodeSpec(sleepSpec)}`

function renderApp(route: string, client = testQueryClient()) {
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return client
}

/** Counts requests per "METHOD /path" (API and snapshot alike). */
function countRequests() {
  const counts = new Map<string, number>()
  server.events.on('request:start', ({ request }) => {
    const key = `${request.method} ${new URL(request.url).pathname}`
    counts.set(key, (counts.get(key) ?? 0) + 1)
  })
  return (key: string) => counts.get(key) ?? 0
}

afterEach(() => {
  server.events.removeAllListeners()
})

/** ServiceNotice's two persistent live regions: [waking, reload]. */
function noticeRegions(): HTMLOutputElement[] {
  return Array.from(
    document.querySelectorAll<HTMLOutputElement>('output.service-notice-region'),
  )
}

describe('cold start (F11, F12)', () => {
  it('announces the waking message after ~3 s without /meta, politely and without moving focus', async () => {
    await withFakeClock(async () => {
      server.use(
        http.get(api('/meta'), async () => {
          await delay(20_000)
          return HttpResponse.json(metaFixture)
        }),
      )
      renderApp('/explore')
      expect(screen.getByText('Loading the chart builder…')).toBeInTheDocument()
      // The live region exists, empty, before its text arrives: text added to
      // a region already in the page is announced; a region mounted together
      // with its text often is not (review round 1).
      const [wakingRegion] = noticeRegions()
      expect(noticeRegions()).toHaveLength(2)
      expect(wakingRegion).toBeEmptyDOMElement()

      await vi.advanceTimersByTimeAsync(2_900)
      expect(screen.queryByText(WAKING_MESSAGE)).not.toBeInTheDocument()
      expect(noticeRegions()[0]).toBe(wakingRegion)

      await vi.advanceTimersByTimeAsync(200)
      const message = screen.getByText(WAKING_MESSAGE)
      // A persistent polite live region (<output> = role "status"), no progress bar.
      expect(message.closest('output')).toBe(wakingRegion)
      expect(wakingRegion).toBeInTheDocument()
      expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
      expect(document.activeElement).toBe(document.body)

      await vi.advanceTimersByTimeAsync(17_000)
      expect(await screen.findByRole('button', { name: 'Analyze' })).toBeInTheDocument()
      expect(screen.queryByText(WAKING_MESSAGE)).not.toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })
  })

  it('shows the message at once while the edge answers service_waking, and recovers by itself', async () => {
    await withFakeClock(async () => {
      let calls = 0
      server.use(
        http.get(api('/meta'), () => {
          calls += 1
          if (calls <= 2) {
            return HttpResponse.json(
              { error: { code: 'service_waking', message: 'Waking.', details: null } },
              { status: 503, headers: { 'Retry-After': '5' } },
            )
          }
          return HttpResponse.json(metaFixture)
        }),
      )
      renderApp('/explore')
      await vi.advanceTimersByTimeAsync(100)
      expect(screen.getByText(WAKING_MESSAGE)).toBeInTheDocument()
      await vi.advanceTimersByTimeAsync(10_000)
      expect(await screen.findByRole('button', { name: 'Analyze' })).toBeInTheDocument()
      expect(calls).toBe(3)
      expect(screen.queryByText(WAKING_MESSAGE)).not.toBeInTheDocument()
    })
  })

  it('a busy pool (503 overloaded) during start-up is waited out, not an error page', async () => {
    await withFakeClock(async () => {
      let calls = 0
      server.use(
        http.get(api('/meta'), () => {
          calls += 1
          return calls <= 3
            ? errorResponse(503, 'overloaded', 'All lanes busy.', { retry_after_seconds: 5 })
            : HttpResponse.json(metaFixture)
        }),
      )
      renderApp('/explore')
      await vi.advanceTimersByTimeAsync(16_000)
      expect(await screen.findByRole('button', { name: 'Analyze' })).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })
  })

  it('a deep link says "Preparing", not "Calculating", while /meta is pending', async () => {
    await withFakeClock(async () => {
      server.use(
        http.get(api('/meta'), async () => {
          await delay(8_000)
          return HttpResponse.json(metaFixture)
        }),
      )
      const requests = countRequests()
      renderApp(sleepRoute)
      await vi.advanceTimersByTimeAsync(4_000)
      expect(screen.getAllByText('Preparing the analysis…').length).toBeGreaterThan(0)
      expect(screen.queryByText('Working out the number…')).not.toBeInTheDocument()
      expect(requests('POST /api/v1/analysis/estimate')).toBe(0)
      await vi.advanceTimersByTimeAsync(5_000)
      expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    })
  })
})

describe('API version check (F25)', () => {
  it('an incompatible api_version is one clear full-page state, with no analysis sent', async () => {
    server.use(
      http.get(api('/meta'), () => HttpResponse.json({ ...metaFixture, api_version: 'v2' })),
    )
    const requests = countRequests()
    renderApp(sleepRoute)
    const heading = await screen.findByRole('heading', {
      name: 'This page and the analysis service are out of step',
    })
    expect(heading).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('version v2')
    expect(screen.getByRole('button', { name: 'Reload the page' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
    expect(requests('POST /api/v1/analysis/estimate')).toBe(0)
    // Exactly one /meta request: an incompatibility is never retried.
    expect(requests('GET /api/v1/meta')).toBe(1)
  })

  it('a /meta whose shape changed does not crash into the error boundary', async () => {
    server.use(
      http.get(api('/meta'), () => HttpResponse.json({ api_version: 'v1', data: null })),
    )
    renderApp('/')
    expect(
      await screen.findByRole('heading', {
        name: 'This page and the analysis service are out of step',
      }),
    ).toBeInTheDocument()
    expect(
      screen.queryByText(/Something went wrong in the application/),
    ).not.toBeInTheDocument()
    // Not "works with analysis API v1, but the service reported version v1" (review round 2).
    expect(screen.getByRole('alert')).not.toHaveTextContent('reported version v1')
    expect(screen.getByRole('alert')).toHaveTextContent(
      'metadata is not in the form this page reads',
    )
  })

  it('an API with no data loaded is "no survey data", with Try again (review round 2)', async () => {
    // The API's own /meta before any load: 200, release "unloaded", no years.
    const user = userEvent.setup()
    let loaded = false
    server.use(
      http.get(api('/meta'), () =>
        HttpResponse.json(
          loaded
            ? metaFixture
            : {
                ...metaFixture,
                data: {
                  ...metaFixture.data,
                  release: 'unloaded',
                  years: [],
                  ingestion_run_id: null,
                },
              },
        ),
      ),
    )
    renderApp('/explore')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('No survey data is loaded')
    expect(
      screen.queryByRole('heading', {
        name: 'This page and the analysis service are out of step',
      }),
    ).not.toBeInTheDocument()
    loaded = true
    await user.click(within(alert).getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('button', { name: 'Analyze' })).toBeInTheDocument()
  })
})

describe('quota exhausted: one honest full-page state', () => {
  it('database_paused on an analysis replaces the page, without a cascade of errors', async () => {
    server.use(
      http.post(api('/analysis/estimate'), () =>
        errorResponse(503, 'database_paused', 'Neon quota exceeded.'),
      ),
    )
    const requests = countRequests()
    renderApp(sleepRoute)
    expect(
      await screen.findByRole('heading', { name: 'The analysis database is paused' }),
    ).toBeInTheDocument()
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(screen.getByRole('alert')).toHaveTextContent(/free hosting/)
    expect(screen.getByRole('link', { name: 'Go to the home page' })).toBeInTheDocument()
    expect(requests('POST /api/v1/analysis/estimate')).toBe(1)
  })

  it('a non-JSON 429 from the edge is the daily request limit, not "a problem in the service"', async () => {
    server.use(
      http.get(
        api('/meta'),
        () =>
          new HttpResponse('<html><h1>Error 1027</h1></html>', {
            status: 429,
            headers: { 'Content-Type': 'text/html' },
          }),
      ),
    )
    renderApp('/explore')
    expect(
      await screen.findByRole('heading', {
        name: 'ATUS Explorer has reached today’s request limit',
      }),
    ).toBeInTheDocument()
    expect(screen.queryByText(/problem in the service/)).not.toBeInTheDocument()
  })

  it('capacity_paused on the home page keeps one page, not a broken home page', async () => {
    server.use(
      http.get(api('/meta'), () =>
        errorResponse(503, 'capacity_paused', 'Monthly budget reached.', { scope: 'monthly' }),
      ),
    )
    renderApp('/')
    expect(
      await screen.findByRole('heading', { name: 'New analyses are paused for now' }),
    ).toBeInTheDocument()
    // Already home: no link to itself.
    expect(screen.queryByRole('link', { name: 'Go to the home page' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })
})

describe('the owner’s pause switch (api_paused)', () => {
  it('an analysis gets one honest page with the date, and is never retried', async () => {
    // Review round 2 (api-paused-owner-switch): a suspended host looked like
    // a cold start (150 s of waking notices); the edge's api_paused is final.
    let posts = 0
    const until = new Date(Date.now() + 12 * 24 * 3600 * 1000).toISOString()
    server.use(
      http.post(api('/analysis/estimate'), () => {
        posts += 1
        return errorResponse(503, 'api_paused', 'Paused.', {
          until,
          reason: 'render_quota',
          retry_after_seconds: 12 * 24 * 3600,
        })
      }),
    )
    renderApp(sleepRoute)
    expect(
      await screen.findByRole('heading', { name: 'The analysis service is paused' }),
    ).toBeInTheDocument()
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('this month’s allowance')
    expect(alert).toHaveTextContent(/\(around .+\)/)
    expect(alert).toHaveTextContent('Starter examples')
    expect(screen.queryByText(WAKING_MESSAGE)).not.toBeInTheDocument()
    expect(posts).toBe(1)
  })
})

describe('analysis errors (F13, F64)', () => {
  it('a rate limit is not retried automatically and counts down its Retry-After', async () => {
    await withFakeClock(async () => {
      let posts = 0
      server.use(
        http.post(api('/analysis/estimate'), () => {
          posts += 1
          return HttpResponse.json(
            {
              error: {
                code: 'rate_limited',
                message: 'Too many.',
                details: { scope: 'client' },
              },
            },
            { status: 429, headers: { 'Retry-After': '30' } },
          )
        }),
      )
      renderApp(sleepRoute)
      const alert = await screen.findByRole('alert')
      expect(alert).toHaveTextContent('Too many new analyses in a short time')
      expect(alert).toHaveTextContent('about 30 seconds')
      const button = within(alert).getByRole('button', { name: 'Try again' })
      expect(button).toBeDisabled()
      expect(button).toHaveTextContent('(30 s)')

      await vi.advanceTimersByTimeAsync(10_000)
      await waitFor(() => expect(button).toHaveTextContent('(20 s)'))
      expect(posts).toBe(1)

      await vi.advanceTimersByTimeAsync(20_000)
      await waitFor(() => expect(button).toBeEnabled())
      expect(button).toHaveTextContent(/^Try again$/)
    })
  })

  it('one transient 503 is retried automatically, honouring Retry-After', async () => {
    await withFakeClock(async () => {
      let posts = 0
      server.use(
        http.post(api('/analysis/estimate'), () => {
          posts += 1
          return posts === 1
            ? HttpResponse.json(
                { error: { code: 'overloaded', message: 'Busy.', details: null } },
                { status: 503, headers: { 'Retry-After': '2' } },
              )
            : HttpResponse.json(estimateSleepFixture, { headers: analysisHeaders() })
        }),
      )
      renderApp(sleepRoute)
      await vi.advanceTimersByTimeAsync(2_500)
      expect(await screen.findByText('8h 49m')).toBeInTheDocument()
      expect(posts).toBe(2)
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })
  })

  it('a second transient 503 is shown, with specific copy', async () => {
    await withFakeClock(async () => {
      let posts = 0
      server.use(
        http.post(api('/analysis/estimate'), () => {
          posts += 1
          return errorResponse(503, 'overloaded', 'Busy.')
        }),
      )
      renderApp(sleepRoute)
      await vi.advanceTimersByTimeAsync(5_000)
      expect(await screen.findByRole('alert')).toHaveTextContent('The analysis service is busy')
      expect(posts).toBe(2)
    })
  })

  it('a proxy 502 page while the host wakes is waited out', async () => {
    await withFakeClock(async () => {
      let posts = 0
      server.use(
        http.post(api('/analysis/estimate'), () => {
          posts += 1
          return posts === 1
            ? new HttpResponse('<html>502 Bad gateway</html>', { status: 502 })
            : HttpResponse.json(estimateSleepFixture, { headers: analysisHeaders() })
        }),
      )
      renderApp(sleepRoute)
      await vi.advanceTimersByTimeAsync(1_500)
      expect(await screen.findByText('8h 49m')).toBeInTheDocument()
      expect(posts).toBe(2)
    })
  })

  it('an analysis timeout is not retried and suggests adjusting', async () => {
    let posts = 0
    server.use(
      http.post(api('/analysis/estimate'), () => {
        posts += 1
        return errorResponse(503, 'analysis_timeout', 'Deadline exceeded.')
      }),
    )
    renderApp(sleepRoute)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('This analysis took too long')
    expect(within(alert).getByRole('link', { name: 'Adjust analysis' })).toBeInTheDocument()
    expect(posts).toBe(1)
  })
})

describe('data release changes (F27)', () => {
  /**
   * ADR-013 step 8: the API already serves `served` while the deployed
   * snapshot, which the page's /meta came from, is the old release. The
   * pointer names `deployedLater` from its second read on (the deploy job
   * published the new snapshot after the page loaded).
   */
  async function releaseSwitch(served: string, deployedLater: string) {
    await installSnapshot({
      get: { '/api/v1/meta': metaFixture, '/api/v1/activities/presets': presetsFixture },
    })
    let pointerReads = 0
    server.use(
      http.get(SNAPSHOT_POINTER_PATH, () => {
        pointerReads += 1
        return HttpResponse.json({
          snapshot_id: SNAPSHOT_ID,
          data_version: pointerReads === 1 ? SNAPSHOT_DATA_VERSION : deployedLater,
          api_version: 'v1',
        })
      }),
      http.get(api('/wake'), () => HttpResponse.json({ origin: 'warm' })),
      http.post(api('/analysis/estimate'), () =>
        HttpResponse.json(estimateSleepFixture, {
          headers: { ...analysisHeaders(), 'X-Data-Version': served },
        }),
      ),
    )
  }

  it('mid-switch, when a reload would load the same old snapshot, does not offer one (review round 1)', async () => {
    // Every uncached analysis, every reload and every new tab showed "Reload
    // so that every number comes from the same release", which reloading
    // could not do until the deploy job published the new snapshot.
    await releaseSwitch('0326:serving-new:aaa', SNAPSHOT_DATA_VERSION)
    renderApp(sleepRoute)
    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    const notice = await screen.findByText(/different data release/)
    expect(notice).toHaveTextContent('reloading the page does not change that yet')
    expect(notice.closest('output')).not.toBeNull()
    expect(screen.queryByRole('button', { name: 'Reload page' })).not.toBeInTheDocument()
  })

  it('a page left open through the switch offers the reload once the new snapshot is published (review round 1 verification)', async () => {
    // The pointer was read once per changed version: a tab that saw the
    // "switching" notice kept it after the deploy job had published the new
    // snapshot, although operations.md step 6.2 says it then offers a reload.
    await withFakeClock(async () => {
      const served = '0326:serving-new:aaa'
      let published = SNAPSHOT_DATA_VERSION
      let pointerReads = 0
      await installSnapshot({
        get: { '/api/v1/meta': metaFixture, '/api/v1/activities/presets': presetsFixture },
      })
      server.use(
        http.get(SNAPSHOT_POINTER_PATH, () => {
          pointerReads += 1
          return HttpResponse.json({
            snapshot_id: SNAPSHOT_ID,
            data_version: published,
            api_version: 'v1',
          })
        }),
        http.get(api('/wake'), () => HttpResponse.json({ origin: 'warm' })),
        http.post(api('/analysis/estimate'), () =>
          HttpResponse.json(estimateSleepFixture, {
            headers: { ...analysisHeaders(), 'X-Data-Version': served },
          }),
        ),
      )
      renderApp(sleepRoute)
      expect(await screen.findByText('8h 49m')).toBeInTheDocument()
      const switching = await screen.findByText(/switching to a different data release/)
      const region = switching.closest('output')
      expect(region).not.toBeNull()
      expect(screen.queryByRole('button', { name: 'Reload page' })).not.toBeInTheDocument()
      const readsWhileSwitching = pointerReads

      // Still mid-switch a minute later: one more read, same notice.
      await vi.advanceTimersByTimeAsync(60_000)
      await waitFor(() => expect(pointerReads).toBe(readsWhileSwitching + 1))
      expect(screen.queryByRole('button', { name: 'Reload page' })).not.toBeInTheDocument()

      // The deploy job publishes the new snapshot: the next read offers the reload,
      // in the same live region.
      published = served
      await vi.advanceTimersByTimeAsync(60_000)
      const reload = await screen.findByRole('button', { name: 'Reload page' })
      expect(reload.closest('output')).toBe(region)
      expect(region).toHaveTextContent(
        'Reload so that every number comes from the same release.',
      )

      // Once a reload helps, the pointer is not read again.
      const readsAfter = pointerReads
      await vi.advanceTimersByTimeAsync(180_000)
      expect(pointerReads).toBe(readsAfter)
    })
  })

  it('once the new snapshot is deployed, offers the reload', async () => {
    await releaseSwitch('0326:serving-new:aaa', '0326:serving-new:aaa')
    renderApp(sleepRoute)
    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'Reload page' })).toBeInTheDocument()
    expect(screen.getByText(/different data release/)).toHaveTextContent(
      'Reload so that every number comes from the same release.',
    )
  })

  it('announces the change in the live region that was already there, empty (review round 1)', async () => {
    server.use(
      http.get(api('/meta'), () =>
        HttpResponse.json(metaFixture, { headers: { 'X-Data-Version': '0325:serving-a:111' } }),
      ),
      http.post(api('/analysis/estimate'), async () => {
        await delay(100)
        return HttpResponse.json(estimateSleepFixture, {
          headers: { ...analysisHeaders(), 'X-Data-Version': '0325:serving-b:222' },
        })
      }),
    )
    renderApp(sleepRoute)
    const reloadRegion = noticeRegions()[1]
    expect(noticeRegions()).toHaveLength(2)
    expect(reloadRegion).toBeEmptyDOMElement()
    const notice = await screen.findByText(/different data release/)
    expect(notice.closest('output')).toBe(reloadRegion)
    expect(reloadRegion).toBeInTheDocument()
  })

  it('offers a reload when a response comes from a different data release', async () => {
    // The page loaded serving-a; the switched API now answers serving-b, its
    // /meta included (what a reload would read).
    let metaReads = 0
    server.use(
      http.get(api('/meta'), () => {
        metaReads += 1
        const version = metaReads === 1 ? '0325:serving-a:111' : '0325:serving-b:222'
        return HttpResponse.json(metaFixture, { headers: { 'X-Data-Version': version } })
      }),
      http.post(api('/analysis/estimate'), () =>
        HttpResponse.json(estimateSleepFixture, {
          headers: { ...analysisHeaders(), 'X-Data-Version': '0325:serving-b:222' },
        }),
      ),
    )
    renderApp(sleepRoute)
    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    const notice = await screen.findByText(/different data release/)
    expect(notice.closest('output')).not.toBeNull()
    expect(screen.getByRole('button', { name: 'Reload page' })).toBeInTheDocument()
    expect(document.activeElement).toBe(document.body)
  })

  it('stays quiet when the versions agree', async () => {
    server.use(
      http.get(api('/meta'), () =>
        HttpResponse.json(metaFixture, { headers: { 'X-Data-Version': '0325:serving-a:111' } }),
      ),
      http.post(api('/analysis/estimate'), () =>
        HttpResponse.json(estimateSleepFixture, {
          headers: { ...analysisHeaders(), 'X-Data-Version': '0325:serving-a:111' },
        }),
      ),
    )
    renderApp(sleepRoute)
    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    expect(screen.queryByText(/different data release/)).not.toBeInTheDocument()
  })

  // Review round 2 (known issue from round 1): without a snapshot the reload
  // check assumed the page's /meta came from the API, which already serves
  // the new release. Through the site the edge Worker answers a plain
  // /api/v1/meta from its own deployed snapshot, so a reload can load the
  // old release again: the check asks what a reload would read.
  it('without a loaded snapshot, offers no reload while the site’s /meta still reports the old release', async () => {
    server.use(
      http.get(api('/meta'), () =>
        HttpResponse.json(metaFixture, { headers: { 'X-Data-Version': '0325:serving-a:111' } }),
      ),
      http.post(api('/analysis/estimate'), () =>
        HttpResponse.json(estimateSleepFixture, {
          headers: { ...analysisHeaders(), 'X-Data-Version': '0325:serving-b:222' },
        }),
      ),
    )
    const requests = countRequests()
    renderApp(sleepRoute)
    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    const notice = await screen.findByText(/switching to a different data release/)
    expect(notice).toHaveTextContent('reloading the page does not change that yet')
    expect(screen.queryByRole('button', { name: 'Reload page' })).not.toBeInTheDocument()
    // The check read the site's /meta (one more GET), never through the HTTP cache.
    expect(requests('GET /api/v1/meta')).toBe(2)
  })

  it('a page whose snapshot manifest failed to load asks the pointer a reload would read', async () => {
    // The pointer still names the old release; this page fell back to the
    // API because the manifest could not be read when it loaded.
    let manifestReads = 0
    server.use(
      http.get(SNAPSHOT_POINTER_PATH, () =>
        HttpResponse.json({
          snapshot_id: SNAPSHOT_ID,
          data_version: '0325:serving-a:111',
          analytics_version: '0.2',
          api_version: 'v1',
        }),
      ),
      http.get(`/snap/${SNAPSHOT_ID}/manifest.json`, () => {
        manifestReads += 1
        return new HttpResponse('unavailable', { status: 503 })
      }),
      http.get(api('/meta'), () =>
        HttpResponse.json(metaFixture, { headers: { 'X-Data-Version': '0325:serving-a:111' } }),
      ),
      http.post(api('/analysis/estimate'), () =>
        HttpResponse.json(estimateSleepFixture, {
          headers: { ...analysisHeaders(), 'X-Data-Version': '0325:serving-b:222' },
        }),
      ),
    )
    renderApp(sleepRoute)
    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    expect(manifestReads).toBe(1)
    expect(await screen.findByText(/switching to a different data release/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reload page' })).not.toBeInTheDocument()
  })

  // Review round 2: only X-Data-Version was compared, so a release that
  // changed only ANALYTICS_VERSION mixed two statistical implementations on
  // one page under the old version tag, without any notice.
  it('a result from another analytics version offers a reload (header)', async () => {
    let metaReads = 0
    server.use(
      http.get(api('/meta'), () => {
        metaReads += 1
        const analytics = metaReads === 1 ? '0.2' : '0.3'
        return HttpResponse.json(
          { ...metaFixture, analytics_version: analytics },
          {
            headers: {
              'X-Data-Version': '0325:serving-a:111',
              'X-Analytics-Version': analytics,
            },
          },
        )
      }),
      http.post(api('/analysis/estimate'), () =>
        HttpResponse.json(
          { ...estimateSleepFixture, analytics_version: '0.3' },
          {
            headers: {
              ...analysisHeaders(),
              'X-Data-Version': '0325:serving-a:111',
              'X-Analytics-Version': '0.3',
            },
          },
        ),
      ),
    )
    renderApp(sleepRoute)
    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    const notice = await screen.findByText(/different version of its statistical methods/)
    expect(notice.closest('output')).toBe(noticeRegions()[1])
    expect(notice).toHaveTextContent('analytics 0.3')
    expect(screen.getByRole('button', { name: 'Reload page' })).toBeInTheDocument()
    expect(screen.queryByText(/different data release/)).not.toBeInTheDocument()
    expect(document.activeElement).toBe(document.body)
  })

  it('a result whose body names another analytics version is noticed too', async () => {
    // A cached or snapshot answer carries its version in the body.
    server.use(
      http.post(api('/analysis/estimate'), () =>
        HttpResponse.json(
          { ...estimateSleepFixture, analytics_version: '0.3' },
          { headers: analysisHeaders() },
        ),
      ),
    )
    renderApp(sleepRoute)
    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    expect(await screen.findByText(/statistical methods/)).toHaveTextContent('analytics 0.3')
  })

  it('mid-switch, an analytics change the snapshot does not have yet offers no reload', async () => {
    // The API was switched first (deploy.yml deploys it before the Worker):
    // a reload would load the deployed snapshot, still analytics 0.2.
    await installSnapshot({
      get: { '/api/v1/meta': metaFixture, '/api/v1/activities/presets': presetsFixture },
    })
    server.use(
      http.get(api('/wake'), () => HttpResponse.json({ origin: 'warm' })),
      http.post(api('/analysis/estimate'), () =>
        HttpResponse.json(
          { ...estimateSleepFixture, analytics_version: '0.3' },
          {
            headers: {
              ...analysisHeaders(),
              'X-Data-Version': SNAPSHOT_DATA_VERSION,
              'X-Analytics-Version': '0.3',
            },
          },
        ),
      ),
    )
    renderApp(sleepRoute)
    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    const notice = await screen.findByText(
      /switching to a different version of its statistical/,
    )
    expect(notice).toHaveTextContent('reloading the page does not change that yet')
    expect(screen.queryByRole('button', { name: 'Reload page' })).not.toBeInTheDocument()
  })
})

describe('no background metadata refetch (F62)', () => {
  it('returning to the tab does not refetch metadata (a refetch could wake a sleeping host)', async () => {
    await withFakeClock(async () => {
      const requests = countRequests()
      renderApp('/explore')
      await screen.findByRole('button', { name: 'Analyze' })
      expect(requests('GET /api/v1/meta')).toBe(1)
      // Before Phase 5, /meta went stale after 5 minutes and refetched on focus.
      await vi.advanceTimersByTimeAsync(30 * 60_000)
      act(() => {
        focusManager.setFocused(false)
        focusManager.setFocused(true)
      })
      act(() => {
        onlineManager.setOnline(false)
        onlineManager.setOnline(true)
      })
      await vi.advanceTimersByTimeAsync(1_000)
      expect(requests('GET /api/v1/meta')).toBe(1)
      expect(requests('GET /api/v1/population/metadata')).toBe(1)
      focusManager.setFocused(undefined)
    })
  })
})

describe('one announcement per state (review round 1)', () => {
  /** Live regions (polite or assertive) whose text matches: each is announced on change. */
  function liveRegionsWith(text: RegExp): Element[] {
    return Array.from(
      document.querySelectorAll('output, [role="status"], [role="alert"], [aria-live]'),
    )
      .filter((element) => element.getAttribute('aria-live') !== 'off')
      .filter((element) => text.test(element.textContent ?? ''))
  }

  it('the waking state is announced by the notice alone, not by three regions', async () => {
    await installSnapshot({
      get: {
        '/api/v1/meta': metaFixture,
        '/api/v1/activities/presets': presetsFixture,
      },
    })
    server.use(
      http.get(api('/wake'), () => HttpResponse.json({ origin: 'waking' })),
      http.post(api('/analysis/estimate'), async () => {
        await delay(1_500)
        return HttpResponse.json(estimateSleepFixture, { headers: analysisHeaders() })
      }),
    )
    renderApp(sleepRoute)
    // The visible loading text still says what the page waits for…
    await waitFor(() =>
      expect(document.querySelector('.loading-panel')).toHaveTextContent(
        'Waiting for the analysis service to wake up…',
      ),
    )
    // …but only the site-wide notice announces the waking state.
    expect(liveRegionsWith(/wak/i)).toEqual([
      screen.getByText(WAKING_MESSAGE).closest('output'),
    ])
    expect(await screen.findByText('8h 49m', {}, { timeout: 4_000 })).toBeInTheDocument()
  })

  it('each loading message is announced once', async () => {
    server.use(
      http.post(api('/analysis/estimate'), async () => {
        await delay(500)
        return HttpResponse.json(estimateSleepFixture, { headers: analysisHeaders() })
      }),
    )
    renderApp(sleepRoute)
    await waitFor(() => expect(liveRegionsWith(/Working out the number…/)).not.toHaveLength(0))
    expect(liveRegionsWith(/Working out the number…/)).toHaveLength(1)
    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    expect(liveRegionsWith(/Analysis ready\./)).toHaveLength(1)
  })
})

describe('focus after "Try again" (review round 1)', () => {
  // "Try again" sends a query without data back to "pending": the panel that
  // held the focused button unmounts, and focus used to fall to <body>.
  it('keeps focus on the analysis heading while it reloads and after the result', async () => {
    const user = userEvent.setup()
    let posts = 0
    server.use(
      http.post(api('/analysis/estimate'), async () => {
        posts += 1
        if (posts === 1) return errorResponse(503, 'data_version_mismatch', 'Switching.')
        await delay(200)
        return HttpResponse.json(estimateSleepFixture, { headers: analysisHeaders() })
      }),
    )
    renderApp(sleepRoute)
    const retry = await screen.findByRole('button', { name: 'Try again' })
    retry.focus()
    await user.keyboard('{Enter}')
    const heading = screen.getByRole('heading', { level: 1 })
    await waitFor(() => expect(document.querySelector('.loading-panel')).not.toBeNull())
    expect(document.activeElement).toBe(heading)
    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    expect(document.activeElement).toBe(heading)
  })

  it('a retry from the full-page paused state lands on the page it returns to', async () => {
    const user = userEvent.setup()
    let paused = true
    server.use(
      http.post(api('/analysis/estimate'), async () => {
        if (paused) return errorResponse(503, 'database_paused', 'Paused.')
        await delay(200)
        return HttpResponse.json(estimateSleepFixture, { headers: analysisHeaders() })
      }),
    )
    renderApp(sleepRoute)
    const retry = await screen.findByRole('button', { name: 'Try again' })
    paused = false
    retry.focus()
    await user.keyboard('{Enter}')
    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    expect(document.activeElement).toBe(screen.getByRole('heading', { level: 1 }))
    expect(document.activeElement).toHaveTextContent('Sleeping')
  })

  it('a retry that ends on the paused page again leaves focus on its heading', async () => {
    const user = userEvent.setup()
    server.use(
      http.post(api('/analysis/estimate'), async () => {
        await delay(100)
        return errorResponse(503, 'database_paused', 'Paused.')
      }),
    )
    renderApp(sleepRoute)
    const retry = await screen.findByRole('button', { name: 'Try again' })
    retry.focus()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(document.querySelector('.loading-panel')).not.toBeNull())
    const heading = await screen.findByRole('heading', {
      name: 'The analysis database is paused',
    })
    await waitFor(() => expect(document.activeElement).toBe(heading))
  })

  it('Explore: a retry from the page-level error lands on "Build an analysis"', async () => {
    const user = userEvent.setup()
    let failing = true
    server.use(
      http.get(api('/meta'), () =>
        failing
          ? errorResponse(500, 'internal_error', 'Boom.')
          : HttpResponse.json(metaFixture),
      ),
    )
    renderApp('/explore')
    const retry = await screen.findByRole('button', { name: 'Try again' })
    failing = false
    retry.focus()
    await user.keyboard('{Enter}')
    const heading = await screen.findByRole('heading', { name: 'Build an analysis' })
    await waitFor(() => expect(document.activeElement).toBe(heading))
  })

  it('Explore: focus stays on the page heading while a retry runs and after it fails again (review round 2)', async () => {
    // Explore's loading and error states had no h1, so useRetryFocus found
    // nothing to focus and focus sat on <body> for the whole retry.
    const user = userEvent.setup()
    let gets = 0
    server.use(
      http.get(api('/meta'), async () => {
        gets += 1
        if (gets > 1) await delay(200)
        return errorResponse(500, 'internal_error', 'Boom.')
      }),
    )
    renderApp('/explore')
    const retry = await screen.findByRole('button', { name: 'Try again' })
    retry.focus()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(document.querySelector('.loading-panel')).not.toBeNull())
    const heading = screen.getByRole('heading', { level: 1, name: 'Build an analysis' })
    expect(document.activeElement).toBe(heading)
    expect(await screen.findByRole('button', { name: 'Try again' })).toBeInTheDocument()
    expect(gets).toBe(2)
    expect(document.activeElement).toBe(
      screen.getByRole('heading', { level: 1, name: 'Build an analysis' }),
    )
  })

  it('Home: a retry of the starter examples lands on their heading', async () => {
    const user = userEvent.setup()
    let failing = true
    server.use(
      http.get(api('/meta'), () =>
        failing
          ? errorResponse(500, 'internal_error', 'Boom.')
          : HttpResponse.json(metaFixture),
      ),
    )
    renderApp('/')
    const retry = await screen.findByRole('button', { name: 'Try again' })
    failing = false
    retry.focus()
    await user.keyboard('{Enter}')
    expect(await screen.findByText('How much do Americans sleep?')).toBeInTheDocument()
    expect(document.activeElement).toBe(
      screen.getByRole('heading', { name: 'Start from a question' }),
    )
  })
})

describe('focus after in-app navigation (review round 2)', () => {
  // Client-side navigation left focus on <body>: nothing said the page had
  // changed, and the next Tab started from the top of the document.
  it('moves focus to the new page’s heading, Home → Explore → result', async () => {
    const user = userEvent.setup()
    renderApp('/')
    const build = await screen.findByRole('link', { name: 'Make your own chart' })
    build.focus()
    await user.keyboard('{Enter}')
    const exploreHeading = await screen.findByRole('heading', {
      level: 1,
      name: 'Build an analysis',
    })
    await waitFor(() => expect(document.activeElement).toBe(exploreHeading))

    // One number: the builder opens on a trend (review round 4).
    await user.click(await screen.findByRole('radio', { name: 'One number' }))
    const analyze = await screen.findByRole('button', { name: 'Analyze' })
    analyze.focus()
    await user.keyboard('{Enter}')
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole('heading', { level: 1 })),
    )
    expect(document.activeElement).toHaveTextContent('Sleeping')
  })

  it('the canonical URL rewrite (a replace) does not move focus', async () => {
    const user = userEvent.setup()
    renderApp('/')
    const starter = await screen.findByRole('link', { name: /How much do Americans sleep\?/ })
    await user.click(starter)
    const share = await screen.findByRole('button', { name: /Copy link|Share/ })
    share.focus()
    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(document.activeElement).toBe(share)
  })

  it('the first page of a visit keeps focus where the browser puts it', async () => {
    renderApp('/explore')
    expect(await screen.findByRole('button', { name: 'Analyze' })).toBeInTheDocument()
    expect(document.activeElement).toBe(document.body)
  })

  // Review round 4: a full-page quota or pause state replaced the analysis
  // page whose heading the navigation had focused, and focus fell to <body>.
  it.each([
    [
      'capacity_paused',
      { scope: 'monthly', budget: 'cu_month' },
      'New analyses are paused for now',
    ],
    [
      'api_paused',
      { until: '2099-01-01T00:00:00Z', reason: 'render_quota', retry_after_seconds: 3600 },
      'The analysis service is paused',
    ],
    ['database_paused', null, 'The analysis database is paused'],
  ] as const)(
    'a %s page that replaces the analysis after Analyze takes the focus (review round 4)',
    async (code, details, name) => {
      const user = userEvent.setup()
      server.use(
        http.post(api('/analysis/estimate'), async () => {
          await delay(150)
          return errorResponse(503, code, 'Paused.', details)
        }),
      )
      renderApp('/explore')
      await user.click(await screen.findByRole('radio', { name: 'One number' }))
      const analyze = await screen.findByRole('button', { name: 'Analyze' })
      analyze.focus()
      await user.keyboard('{Enter}')
      // The navigation focused the analysis heading first.
      await waitFor(() => expect(document.activeElement).toHaveTextContent(/^Sleeping/))
      const heading = await screen.findByRole('heading', { level: 1, name })
      await waitFor(() => expect(document.activeElement).toBe(heading))
    },
  )

  it('a paused page that the first page of a visit shows does not move focus', async () => {
    server.use(
      http.post(api('/analysis/estimate'), () =>
        errorResponse(503, 'capacity_paused', 'Paused.', { scope: 'monthly' }),
      ),
    )
    renderApp(sleepRoute)
    expect(
      await screen.findByRole('heading', { name: 'New analyses are paused for now' }),
    ).toBeInTheDocument()
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(document.activeElement).toBe(document.body)
  })
})

describe('offline (review round 1)', () => {
  /** The browser's own offline state, as its 'offline' event leaves it. */
  function goOffline() {
    vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false)
    act(() => onlineManager.setOnline(false))
  }

  afterEach(() => {
    vi.restoreAllMocks()
    onlineManager.setOnline(true)
  })

  it('a new analysis started offline says so instead of "Calculating…" forever', async () => {
    // TanStack Query's default networkMode ('online') pauses a query that
    // starts offline before its queryFn runs: no request, no error, and the
    // loading panel stayed up for as long as the browser was offline.
    const user = userEvent.setup()
    const requests = countRequests()
    renderApp('/')
    const starter = await screen.findByRole('link', { name: /How much do Americans sleep\?/ })
    goOffline()
    server.use(http.post(api('/analysis/estimate'), () => HttpResponse.error()))
    await user.click(starter)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('could not be reached')
    expect(alert).toHaveTextContent('Check your internet connection')
    expect(screen.queryByText('Working out the number…')).not.toBeInTheDocument()
    // Offline is final: one attempt, not the cold-start budget.
    expect(requests('POST /api/v1/analysis/estimate')).toBe(1)

    // Back online, the failed analysis runs by itself. (networkMode 'always'
    // turns TanStack Query's refetchOnReconnect off unless it is set.)
    server.resetHandlers()
    vi.restoreAllMocks()
    act(() => onlineManager.setOnline(true))
    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('a page opened offline says so instead of "Preparing…" forever', async () => {
    goOffline()
    server.use(http.get(api('/meta'), () => HttpResponse.error()))
    renderApp(sleepRoute)
    expect(await screen.findByRole('alert')).toHaveTextContent('Check your internet connection')
    server.resetHandlers()
    vi.restoreAllMocks()
    act(() => onlineManager.setOnline(true))
    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    expect(screen.queryByText('Preparing the analysis…')).not.toBeInTheDocument()
  })

  /** The browser goes offline and back online (a Wi-Fi change, a laptop waking up). */
  async function reconnect(times: number) {
    for (let index = 0; index < times; index += 1) {
      act(() => {
        onlineManager.setOnline(false)
        onlineManager.setOnline(true)
      })
      await new Promise((resolve) => setTimeout(resolve, 100))
    }
  }

  // Review round 2: refetchOnReconnect re-sent every analysis that had
  // failed, final answers included — another full analysis_timeout query, a
  // rate-limited request while its "Try again" countdown still ran.
  it.each([
    ['analysis_timeout', 503, { scope: 'global' }, '60'],
    ['rate_limited', 429, { scope: 'client' }, '60'],
    ['daily_capacity_reached', 503, { scope: 'daily', budget: 'global' }, null],
    ['capacity_paused', 503, { scope: 'monthly', budget: 'cu_month' }, null],
    ['database_paused', 503, null, '3600'],
    ['capacity_unavailable', 503, null, '60'],
    ['data_version_mismatch', 503, null, '30'],
    ['insufficient_data', 422, null, null],
  ] as const)(
    'a final %s is not sent again when the browser comes back online (review round 2)',
    async (code, status, details, retryAfter) => {
      let posts = 0
      server.use(
        http.post(api('/analysis/estimate'), () => {
          posts += 1
          return HttpResponse.json(
            { error: { code, message: 'Refused.', details } },
            { status, headers: retryAfter === null ? {} : { 'Retry-After': retryAfter } },
          )
        }),
      )
      renderApp(sleepRoute)
      await screen.findByRole('alert')
      expect(posts).toBe(1)
      await reconnect(3)
      expect(posts).toBe(1)
    },
  )

  it('an incompatible /meta is not fetched again when the browser comes back online (review round 2)', async () => {
    server.use(
      http.get(api('/meta'), () => HttpResponse.json({ ...metaFixture, api_version: 'v2' })),
    )
    const requests = countRequests()
    renderApp('/explore')
    await screen.findByRole('heading', {
      name: 'This page and the analysis service are out of step',
    })
    expect(requests('GET /api/v1/meta')).toBe(1)
    await reconnect(2)
    expect(requests('GET /api/v1/meta')).toBe(1)
  })
})

describe('background failures and retries (F26, F63)', () => {
  it('a failed background /meta refetch keeps the builder and its edits', async () => {
    const user = userEvent.setup()
    const client = renderApp('/explore')
    await screen.findByRole('button', { name: 'Analyze' })
    await user.selectOptions(screen.getByLabelText('Add a filter'), 'sex')
    await user.selectOptions(screen.getByLabelText('Sex'), 'female')

    server.use(http.get(api('/meta'), () => errorResponse(500, 'internal_error', 'Boom.')))
    await client.refetchQueries({ queryKey: ['meta'] })

    expect(screen.getByRole('button', { name: 'Analyze' })).toBeInTheDocument()
    expect(screen.getByLabelText('Sex')).toHaveValue('female')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('a failed background /meta refetch keeps a displayed result', async () => {
    const client = renderApp(sleepRoute)
    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    server.use(http.get(api('/meta'), () => errorResponse(500, 'internal_error', 'Boom.')))
    await client.refetchQueries({ queryKey: ['meta'] })
    expect(screen.getByText('8h 49m')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('"Try again" refetches every failed query on Explore', async () => {
    const user = userEvent.setup()
    let failing = true
    const requests = countRequests()
    server.use(
      http.get(api('/meta'), () =>
        failing
          ? errorResponse(500, 'internal_error', 'Boom.')
          : HttpResponse.json(metaFixture),
      ),
      http.get(api('/population/metadata'), () =>
        failing
          ? errorResponse(500, 'internal_error', 'Boom.')
          : HttpResponse.json(populationMetadataFixture),
      ),
    )
    renderApp('/explore')
    await screen.findByRole('alert')
    await waitFor(() => expect(requests('GET /api/v1/population/metadata')).toBe(1))

    failing = false
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('button', { name: 'Analyze' })).toBeInTheDocument()
    expect(requests('GET /api/v1/meta')).toBe(2)
    expect(requests('GET /api/v1/population/metadata')).toBe(2)
  })

  // Review round 2: the picker said "Close and reopen to retry", but its
  // query stayed mounted (staleTime Infinity, no retries, no focus
  // refetch), so neither reopening nor anything else sent a request.
  async function openFailedPicker() {
    const user = userEvent.setup()
    let failing = true
    const requests = countRequests()
    server.use(
      http.get(api('/activities'), () =>
        failing
          ? errorResponse(500, 'internal_error', 'Boom.')
          : HttpResponse.json(activitiesFixture),
      ),
    )
    renderApp('/explore')
    await screen.findByRole('button', { name: 'Analyze' })
    await user.click(screen.getByRole('button', { name: 'Change activity' }))
    expect(await screen.findByText(/The activity list could not be loaded/)).toBeInTheDocument()
    const sent = requests('GET /api/v1/activities')
    return {
      user,
      sent,
      requests,
      recover: () => {
        failing = false
      },
    }
  }

  it('the activity picker’s "Try again" loads a failed activity list', async () => {
    const { user, sent, requests, recover } = await openFailedPicker()
    recover()
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByLabelText('Search activities')).toBeInTheDocument()
    expect(requests('GET /api/v1/activities')).toBe(sent + 1)
    // Focus stays next to the panel, on its toggle, not on <body>.
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close' }))
  })

  it('reopening the activity picker retries a failed activity list', async () => {
    const { user, sent, requests, recover } = await openFailedPicker()
    recover()
    await user.click(screen.getByRole('button', { name: 'Close' }))
    await user.click(screen.getByRole('button', { name: 'Change activity' }))
    expect(await screen.findByLabelText('Search activities')).toBeInTheDocument()
    expect(requests('GET /api/v1/activities')).toBe(sent + 1)
  })

  it('the home page offers "Try again" when starter examples cannot load', async () => {
    const user = userEvent.setup()
    let failing = true
    server.use(
      http.get(api('/meta'), () =>
        failing
          ? errorResponse(500, 'internal_error', 'Boom.')
          : HttpResponse.json(metaFixture),
      ),
    )
    renderApp('/')
    expect(
      await screen.findByText(/Starter examples need the analysis service/),
    ).toBeInTheDocument()
    failing = false
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('How much do Americans sleep?')).toBeInTheDocument()
  })
})

describe('static snapshot and prewake', () => {
  async function installMetadataSnapshot() {
    return installSnapshot({
      get: {
        '/api/v1/meta': metaFixture,
        '/api/v1/population/metadata': populationMetadataFixture,
        '/api/v1/activities': activitiesFixture,
        '/api/v1/activities/presets': presetsFixture,
      },
    })
  }

  it('entering Explore builds from the snapshot and prewakes the API once', async () => {
    await installMetadataSnapshot()
    let wakes = 0
    server.use(
      http.get(api('/wake'), () => {
        wakes += 1
        return HttpResponse.json({ origin: 'waking' })
      }),
    )
    const requests = countRequests()
    renderApp('/explore')
    expect(await screen.findByRole('button', { name: 'Analyze' })).toBeInTheDocument()
    await waitFor(() => expect(wakes).toBe(1))
    expect(requests('GET /api/v1/meta')).toBe(0)
    expect(requests('GET /api/v1/population/metadata')).toBe(0)
    // Nothing is waiting on the API, so no waking notice yet.
    expect(screen.queryByText(WAKING_MESSAGE)).not.toBeInTheDocument()
  })

  it('without a snapshot (development, E2E) no prewake is sent', async () => {
    const requests = countRequests()
    renderApp('/explore')
    expect(await screen.findByRole('button', { name: 'Analyze' })).toBeInTheDocument()
    expect(requests('GET /api/v1/wake')).toBe(0)
  })

  it('a share link the snapshot lacks says "waking" while the woken API computes', async () => {
    await installMetadataSnapshot()
    server.use(
      http.get(api('/wake'), () => HttpResponse.json({ origin: 'waking' })),
      http.post(api('/analysis/estimate'), async () => {
        await delay(300)
        return HttpResponse.json(estimateSleepFixture, { headers: analysisHeaders() })
      }),
    )
    renderApp(sleepRoute)
    expect(await screen.findByText(WAKING_MESSAGE)).toBeInTheDocument()
    expect(
      screen.getAllByText('Waiting for the analysis service to wake up…').length,
    ).toBeGreaterThan(0)
    expect(await screen.findByText('8h 49m')).toBeInTheDocument()
    expect(screen.queryByText(WAKING_MESSAGE)).not.toBeInTheDocument()
  })

  // Review round 2: "Adjust analysis" then Analyze on a comparison starter
  // sends the builder's form of it (every default stated), which no prewarm
  // entry held, so it went to the API although the snapshot had the result.
  // This drives the real pages against a snapshot of the prewarm list, so it
  // also catches the builder and the list drifting apart.
  it.each(
    STARTER_EXAMPLES.filter((starter) => starter.operation === 'compare').map(
      (starter) => starter.question,
    ),
  )(
    'the starter "%s" re-run unchanged from the builder is served by the snapshot',
    async (question) => {
      const years = metaFixture.data.years
      const prewarmed = prewarmedAnalyses({
        years,
        presets: metaFixture.capabilities.activity_presets,
        measures: metaFixture.capabilities.measures.map((measure) => measure.name),
        weightSchemes: metaFixture.capabilities.weight_schemes.map((scheme) => scheme.name),
        tier1: activitiesFixture.activities
          .filter((activity) => activity.level === 1)
          .map((activity) => ({ code: activity.code, name: activity.name })),
      })
      const requested = await installSnapshot({
        get: {
          '/api/v1/meta': metaFixture,
          '/api/v1/population/metadata': populationMetadataFixture,
          '/api/v1/activities': activitiesFixture,
          '/api/v1/activities/presets': presetsFixture,
        },
        post: prewarmed
          .filter((item) => item.operation === 'compare')
          .map((item) => ({
            path: '/api/v1/analysis/compare',
            body: stableStringify(item.spec),
            response: compareChildrenFixture,
          })),
      })
      server.use(http.get(api('/wake'), () => HttpResponse.json({ origin: 'warm' })))
      const requests = countRequests()
      const user = userEvent.setup()
      renderApp('/')

      await user.click(
        await screen.findByRole('link', { name: (name) => name.includes(question) }),
      )
      const barLabel = () =>
        screen.findByText(compareChildrenFixture.label_a, { selector: '.compare-chart__label' })
      expect(await barLabel()).toBeInTheDocument()
      await user.click(screen.getByRole('link', { name: 'Adjust analysis' }))
      await user.click(await screen.findByRole('button', { name: 'Analyze' }))
      expect(await barLabel()).toBeInTheDocument()

      // Snapshot entries only, no API call: the home page's previews of the
      // comparison starters (the clicked starter's bytes among them), then
      // the builder's bytes.
      const compareStarters = STARTER_EXAMPLES.filter(
        (starter) => starter.operation === 'compare',
      )
      const postFiles = new Set(requested.filter((file) => file.startsWith('post/')))
      expect(postFiles.size).toBe(compareStarters.length + 1)
      expect(requests('POST /api/v1/analysis/compare')).toBe(0)
    },
  )

  it('the home page previews each starter answer from the snapshot, never the API', async () => {
    const years = metaFixture.data.years
    const latest = Math.max(...years)
    const responses = {
      estimate: estimateSleepFixture,
      trend: trendLeisureFixture,
      compare: compareChildrenFixture,
    }
    await installSnapshot({
      get: { '/api/v1/meta': metaFixture },
      post: STARTER_EXAMPLES.map((starter) => ({
        path: `/api/v1/analysis/${starter.operation}`,
        body: stableStringify(starter.spec(years, latest)),
        response: responses[starter.operation],
      })),
    })
    const requests = countRequests()
    renderApp('/')

    const sleep = await screen.findByRole('link', { name: /How much do Americans sleep\?/ })
    await waitFor(() => expect(sleep).toHaveTextContent('8h 49m a day in 2025'))
    const tv = screen.getByRole('link', { name: /Who watches more TV/ })
    await waitFor(() => expect(tv).toHaveTextContent(compareChildrenFixture.label_a))
    for (const operation of ['estimate', 'trend', 'compare']) {
      expect(requests(`POST /api/v1/analysis/${operation}`)).toBe(0)
    }
  })

  it('the home page draws its split-trend chart from the snapshot alone (review round 4)', async () => {
    const years = metaFixture.data.years
    const home = homeChartSpecs(years)
    const perGroup = (sex: string) => ({
      ...trendLeisureFixture,
      spec: { ...trendLeisureFixture.spec, population: { sex } },
    })
    await installSnapshot({
      get: { '/api/v1/meta': metaFixture },
      post: home.series.flatMap((series) => [
        {
          path: '/api/v1/analysis/trend',
          body: stableStringify(series.spec),
          response: perGroup(series.key),
        },
        ...(series.partial2020
          ? [
              {
                path: '/api/v1/analysis/estimate',
                body: stableStringify(series.partial2020),
                response: estimatePandemicFixture,
              },
            ]
          : []),
      ]),
    })
    const requests = countRequests()
    renderApp('/')
    const chart = await screen.findByRole('region', {
      name: /^Time on household activities, men and women/,
    })
    expect(within(chart).getByText(/^In 2025, men spent /)).toBeInTheDocument()
    expect(within(chart).getByRole('application')).toBeInTheDocument()
    expect(
      within(chart).getByText('2020 (hollow points) is a partial year.', { exact: false }),
    ).toBeInTheDocument()
    expect(within(chart).getByRole('link', { name: 'Open this chart' })).toHaveAttribute(
      'href',
      expect.stringContaining('&split=sex&groups=male,female'),
    )
    for (const operation of ['estimate', 'trend', 'compare']) {
      expect(requests(`POST /api/v1/analysis/${operation}`)).toBe(0)
    }
  })

  it('without a snapshot the home page shows the questions alone and calls no analysis', async () => {
    const requests = countRequests()
    renderApp('/')
    const sleep = await screen.findByRole('link', { name: /How much do Americans sleep\?/ })
    expect(sleep).not.toHaveTextContent('8h 49m')
    // Nor its chart.
    expect(screen.queryByRole('region', { name: /^Time on household activities/ })).toBeNull()
    for (const operation of ['estimate', 'trend', 'compare']) {
      expect(requests(`POST /api/v1/analysis/${operation}`)).toBe(0)
    }
  })
})
