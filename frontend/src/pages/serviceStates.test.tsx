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
import { encodeSpec } from '../domain/urlSpec'
import {
  activitiesFixture,
  estimateSleepFixture,
  metaFixture,
  populationMetadataFixture,
  presetsFixture,
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
      expect(screen.getByText('Loading the analysis builder…')).toBeInTheDocument()
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
      expect(await screen.findByText('Build an analysis')).toBeInTheDocument()
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
      expect(await screen.findByText('Build an analysis')).toBeInTheDocument()
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
      expect(await screen.findByText('Build an analysis')).toBeInTheDocument()
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
      expect(screen.queryByText('Calculating estimate…')).not.toBeInTheDocument()
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
})

describe('no background metadata refetch (F62)', () => {
  it('returning to the tab does not refetch metadata (a refetch could wake a sleeping host)', async () => {
    await withFakeClock(async () => {
      const requests = countRequests()
      renderApp('/explore')
      await screen.findByText('Build an analysis')
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
    await waitFor(() => expect(liveRegionsWith(/Calculating estimate…/)).not.toHaveLength(0))
    expect(liveRegionsWith(/Calculating estimate…/)).toHaveLength(1)
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
    expect(screen.queryByText('Calculating estimate…')).not.toBeInTheDocument()
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
})

describe('background failures and retries (F26, F63)', () => {
  it('a failed background /meta refetch keeps the builder and its edits', async () => {
    const user = userEvent.setup()
    const client = renderApp('/explore')
    await screen.findByText('Build an analysis')
    await user.selectOptions(screen.getByLabelText('Sex'), 'female')

    server.use(http.get(api('/meta'), () => errorResponse(500, 'internal_error', 'Boom.')))
    await client.refetchQueries({ queryKey: ['meta'] })

    expect(screen.getByText('Build an analysis')).toBeInTheDocument()
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
    expect(await screen.findByText('Build an analysis')).toBeInTheDocument()
    expect(requests('GET /api/v1/meta')).toBe(2)
    expect(requests('GET /api/v1/population/metadata')).toBe(2)
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
    expect(await screen.findByText('Build an analysis')).toBeInTheDocument()
    await waitFor(() => expect(wakes).toBe(1))
    expect(requests('GET /api/v1/meta')).toBe(0)
    expect(requests('GET /api/v1/population/metadata')).toBe(0)
    // Nothing is waiting on the API, so no waking notice yet.
    expect(screen.queryByText(WAKING_MESSAGE)).not.toBeInTheDocument()
  })

  it('without a snapshot (development, E2E) no prewake is sent', async () => {
    const requests = countRequests()
    renderApp('/explore')
    expect(await screen.findByText('Build an analysis')).toBeInTheDocument()
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
})
