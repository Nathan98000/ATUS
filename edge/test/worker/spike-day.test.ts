/**
 * The budget model's spike day (docs/deployment.md, "Realistic demo
 * traffic": 300 visits over about 12 waking hours, about one uncached
 * analysis each) played through the Worker against the governor, with a
 * controlled clock and an origin that answers every analysis with 1 s of
 * DB time.
 *
 * The governor charges each admitted analysis at NEON_MAX_CU (2 CU) for its
 * DB time plus the whole NEON_IDLE_TAIL_MS (300 s). Analyses 144 s apart
 * arrive inside the previous tail, so the charge never stops, and the daily
 * compute ceiling (2.903 CU-h without ground truth, 10 CU-h with it) refuses
 * most of the day with 503 daily_capacity_reached (budget cu_day).
 * docs/deployment.md ("The spike day under the governor") and ADR-015
 * ("Consequences") quote these results; tests/deploy/test_docs_accuracy.py
 * holds the docs to the numbers asserted here.
 */

import { runInDurableObject } from 'cloudflare:test'
import { env } from 'cloudflare:workers'
import { beforeEach, describe, expect, it } from 'vitest'
import type { Governor } from '../../src/governor'
import {
  apiJson,
  callWorker,
  controlClock,
  governorSql,
  governorStub,
  mockOrigin,
  readEnvelope,
  resetEdgeState,
} from './helpers'

// 06:00 UTC: the spike's 12 waking hours end before midnight, so every
// analysis is charged to one UTC day.
const START = Date.UTC(2036, 9, 20, 6, 0, 0)
const SPIKE_ANALYSES = 300
const SPACING_MS = 144_000 // 300 analyses spread evenly over 12 h
const PATH = '/api/v1/analysis/estimate'
const COMPUTED = { 'x-cache': 'miss', 'server-timing': 'db;dur=1000, app;dur=5' }

/** Give the governor a Neon key and project (or take them away). */
async function setNeon(configured: boolean): Promise<void> {
  await runInDurableObject(governorStub(), (instance: Governor) => {
    const target = instance as unknown as { env: Env }
    target.env = configured
      ? { ...env, NEON_API_KEY: 'neon-test-key', NEON_PROJECT_ID: 'spike-day-project' }
      : env
  })
}

interface SpikeResult {
  mode: string
  admitted: number
  firstDenialAfterS: number | null
  budgets: Record<string, number>
}

/** 300 uncached single-year analyses, one every 144 s, each from its own /24. */
async function playSpike(): Promise<SpikeResult> {
  const clock = controlClock(START)
  mockOrigin(() => apiJson('{"ok":true}', 200, COMPUTED))
  let admitted = 0
  let firstDenialAfterS: number | null = null
  const budgets: Record<string, number> = {}
  for (let i = 0; i < SPIKE_ANALYSES; i += 1) {
    const response = await callWorker(PATH, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'cf-connecting-ip': `100.${64 + (i >> 8)}.${i & 255}.1`,
      },
      body: `{"years":[2024],"v":${i}}`,
    })
    if (response.status === 200) {
      admitted += 1
    } else {
      expect(response.status).toBe(503)
      const error = await readEnvelope(response)
      expect(error.code).toBe('daily_capacity_reached')
      const budget = String(error.details?.budget)
      budgets[budget] = (budgets[budget] ?? 0) + 1
      firstDenialAfterS ??= (i * SPACING_MS) / 1000
    }
    clock.advance(SPACING_MS)
  }
  const report = await governorStub().budget()
  return { mode: report.mode, admitted, firstDenialAfterS, budgets }
}

beforeEach(async () => {
  await resetEdgeState()
  await setNeon(false)
})

describe('the spike day of the budget model', () => {
  it('conservative mode (no Neon key) serves 34 of its 300 uncached analyses', async () => {
    expect(await playSpike()).toEqual({
      mode: 'conservative',
      admitted: 34,
      firstDenialAfterS: 4_896,
      budgets: { cu_day: 266 },
    })
  })

  it('fresh ground truth (mid-period, well under the pacing line) serves 123 of them', async () => {
    await setNeon(true)
    const neon = {
      polledAt: START,
      computeTimeSeconds: 20 * 3600,
      dataTransferBytes: 1e8,
      periodStart: Date.UTC(2036, 9, 5),
      periodEnd: Date.UTC(2036, 10, 5),
      cuBaseS: 0,
      egressBase: 0,
      cuAtPollS: 0,
      egressAtPoll: 0,
    }
    await governorSql('INSERT INTO state (id, value) VALUES (1, ?)', JSON.stringify({ neon }))
    expect(await playSpike()).toEqual({
      mode: 'ground_truth',
      admitted: 123,
      firstDenialAfterS: 17_712,
      budgets: { cu_day: 177 },
    })
  })
})
