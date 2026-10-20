/**
 * Static snapshot consumption (ADR-011 L0): metadata and listed analyses come
 * from static files keyed on the exact request bytes; anything else — or any
 * problem with the snapshot — goes to the API.
 */
import { createHash } from 'node:crypto'

import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'

import type { EstimateRequest } from './types'
import { estimateSleepFixture, metaFixture } from '../test/fixtures'
import {
  SNAPSHOT_DATA_VERSION,
  SNAPSHOT_ID,
  analysisHeaders,
  api,
  installSnapshot,
  server,
} from '../test/server'
import { stableStringify } from '../utils/stableStringify'
import { fetchMeta, runAnalysis } from './endpoints'
import { SNAPSHOT_POINTER_PATH, requestKey } from './snapshot'

const sleepSpec: EstimateRequest = {
  measure: 'average_minutes_per_day',
  activity: { preset: 'sleep' },
  years: [2025],
  population: { age_min: 25, age_max: 54 },
  weights: 'multiyear',
  variance: 'replicate',
  confidence_level: 0.95,
}

afterEach(() => {
  server.events.removeAllListeners()
})

/** Counts API calls per method+path. */
function countApiCalls() {
  const counts = new Map<string, number>()
  server.events.on('request:start', ({ request }) => {
    const url = new URL(request.url)
    if (!url.pathname.startsWith('/api/')) return
    const key = `${request.method} ${url.pathname}`
    counts.set(key, (counts.get(key) ?? 0) + 1)
  })
  return (key: string) => counts.get(key) ?? 0
}

describe('without a snapshot', () => {
  it('reads metadata from the API when /snap/current.json is missing', async () => {
    const calls = countApiCalls()
    const loaded = await fetchMeta()
    expect(loaded.meta.api_version).toBe('v1')
    expect(calls('GET /api/v1/meta')).toBe(1)
  })

  it('treats the SPA fallback (index.html, 200) as no snapshot', async () => {
    server.use(
      http.get(
        SNAPSHOT_POINTER_PATH,
        () =>
          new HttpResponse('<!doctype html><div id="root"></div>', {
            headers: { 'Content-Type': 'text/html' },
          }),
      ),
    )
    const calls = countApiCalls()
    await fetchMeta()
    expect(calls('GET /api/v1/meta')).toBe(1)
  })
})

describe('with a snapshot', () => {
  it('serves metadata from the snapshot, tagged with its data version', async () => {
    await installSnapshot({ get: { '/api/v1/meta': metaFixture } })
    const calls = countApiCalls()
    const loaded = await fetchMeta()
    expect(loaded.meta).toEqual(metaFixture)
    expect(loaded.dataVersion).toBe(SNAPSHOT_DATA_VERSION)
    expect(calls('GET /api/v1/meta')).toBe(0)
  })

  it('serves a listed analysis from the snapshot, keyed on the exact body bytes', async () => {
    await installSnapshot({
      post: [
        {
          path: '/api/v1/analysis/estimate',
          body: stableStringify(sleepSpec),
          response: estimateSleepFixture,
          analysisKey: 'c'.repeat(64),
        },
      ],
    })
    const calls = countApiCalls()
    // Same spec, different key insertion order: the stable serializer gives
    // the same bytes, so the same snapshot entry.
    const reordered = Object.fromEntries(Object.entries(sleepSpec).reverse()) as EstimateRequest
    const result = await runAnalysis('estimate', reordered)
    expect(result.data).toEqual(estimateSleepFixture)
    expect(result.analysisKey).toBe('c'.repeat(64))
    expect(result.cache).toBe('snapshot')
    expect(result.dataVersion).toBe(SNAPSHOT_DATA_VERSION)
    // Neither the analysis nor a prewake reached the API.
    expect(calls('POST /api/v1/analysis/estimate')).toBe(0)
    expect(calls('GET /api/v1/wake')).toBe(0)
  })

  it('sends an unlisted analysis to the API and prewakes it', async () => {
    await installSnapshot({ get: { '/api/v1/meta': metaFixture } })
    let wakes = 0
    server.use(
      http.get(api('/wake'), () => {
        wakes += 1
        return HttpResponse.json({ origin: 'warm' })
      }),
    )
    const calls = countApiCalls()
    const result = await runAnalysis('estimate', sleepSpec)
    expect(result.cache).toBe('miss')
    expect(calls('POST /api/v1/analysis/estimate')).toBe(1)
    await expect.poll(() => wakes).toBe(1)
  })

  it('falls back to the API when a listed file turns out to be missing', async () => {
    await installSnapshot({
      post: [
        {
          path: '/api/v1/analysis/estimate',
          body: stableStringify(sleepSpec),
          response: estimateSleepFixture,
        },
      ],
    })
    const key = (await requestKey('/api/v1/analysis/estimate', '', stableStringify(sleepSpec)))!
    server.use(
      http.get(
        `/snap/${SNAPSHOT_ID}/post/${key}.json`,
        () => new HttpResponse('<!doctype html>'),
      ),
      http.get(api('/wake'), () => HttpResponse.json({ origin: 'warm' })),
      http.post(api('/analysis/estimate'), () =>
        HttpResponse.json(estimateSleepFixture, { headers: analysisHeaders('hit') }),
      ),
    )
    const result = await runAnalysis('estimate', sleepSpec)
    expect(result.cache).toBe('hit')
  })

  it('ignores a snapshot written for another API major version', async () => {
    await installSnapshot({ get: { '/api/v1/meta': metaFixture } })
    server.use(
      http.get(SNAPSHOT_POINTER_PATH, () =>
        HttpResponse.json({ snapshot_id: SNAPSHOT_ID, data_version: 'x', api_version: 'v2' }),
      ),
    )
    const calls = countApiCalls()
    await fetchMeta()
    expect(calls('GET /api/v1/meta')).toBe(1)
  })

  it('ignores manifest entries with unsafe file names', async () => {
    server.use(
      http.get(SNAPSHOT_POINTER_PATH, () =>
        HttpResponse.json({ snapshot_id: SNAPSHOT_ID, data_version: 'v', api_version: 'v1' }),
      ),
      http.get(`/snap/${SNAPSHOT_ID}/manifest.json`, () =>
        HttpResponse.json({
          snapshot_id: SNAPSHOT_ID,
          data_version: 'v',
          get: { '/api/v1/meta': '../../evil.json' },
          post: {},
        }),
      ),
    )
    const calls = countApiCalls()
    await fetchMeta()
    expect(calls('GET /api/v1/meta')).toBe(1)
  })
})

describe('requestKey', () => {
  it('is sha256_hex(path + "?" + query + "\\n" + body), as the edge computes it', async () => {
    const body = stableStringify(sleepSpec)
    const expected = createHash('sha256')
      .update(`/api/v1/analysis/estimate?\n${body}`, 'utf8')
      .digest('hex')
    expect(await requestKey('/api/v1/analysis/estimate', '', body)).toBe(expected)
  })

  it('hashes UTF-8 bytes (non-ASCII labels)', async () => {
    const body = stableStringify({
      ...sleepSpec,
      activity: { include: ['0101'], label: 'Schlaf – Ruhe' },
    })
    const expected = createHash('sha256')
      .update(`/api/v1/analysis/estimate?\n${body}`, 'utf8')
      .digest('hex')
    expect(await requestKey('/api/v1/analysis/estimate', '', body)).toBe(expected)
  })
})
