#!/usr/bin/env node
/**
 * Prints the analysis requests the static snapshot prewarms (ADR-011, L0),
 * as a JSON array on stdout:
 *
 *   [{ "name": "...", "path": "/api/v1/analysis/estimate", "body": "<exact bytes>", "key": "<hex>" }]
 *
 * `body` is the exact request body the SPA sends for that analysis — the
 * spec serialized with the SPA's own `stableStringify` (src/utils) — so the
 * snapshot builder must send it unchanged, never re-serialize it. `key` is
 * `sha256_hex(path + "?" + query + "\n" + body)` with an empty query: the
 * manifest key the SPA computes for the same bytes. The specs come from
 * src/domain/prewarm.ts, which builds them with the analysis builder's own
 * code, so the list and the UI cannot drift apart.
 *
 * Two passes, both driven by the snapshot builder (scripts/build_snapshot.py):
 *
 *   1. node scripts/snapshot-requests.mjs --meta meta.json --activities activities.json
 *      meta.json / activities.json: the release's GET /api/v1/meta and
 *      GET /api/v1/activities response bodies.
 *
 *   2. node scripts/snapshot-requests.mjs --canonical responses.json
 *      responses.json: [{ "path": ..., "response": <analysis response body> }]
 *      from pass 1. Prints the requests for each response's canonical `spec`
 *      (estimate and trend responses carry one). The SPA rewrites a result's
 *      URL to that canonical spec, so share links copied from a prewarmed
 *      result send these bytes; prewarming them makes those links hit too.
 *      Requests already printed by pass 1 can be skipped by key.
 *
 * Needs Node >= 22.18 (it imports the SPA's TypeScript modules directly).
 */
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { parseArgs } from 'node:util'

import { prewarmedAnalyses } from '../src/domain/prewarm.ts'
import { stableStringify } from '../src/utils/stableStringify.ts'

/** The SPA's API_VERSION_PREFIX (src/api/client.ts; a test keeps them equal). */
const API_PREFIX = '/api/v1'

function fail(message) {
  process.stderr.write(`snapshot-requests: ${message}\n`)
  process.exit(2)
}

function readJson(file) {
  try {
    return JSON.parse(readFileSync(file, 'utf8'))
  } catch (error) {
    fail(`cannot read ${file}: ${error.message}`)
  }
}

function requestKey(path, body) {
  return createHash('sha256').update(`${path}?\n${body}`, 'utf8').digest('hex')
}

function entry(name, path, body) {
  return { name, path, body, key: requestKey(path, body) }
}

function prewarmPass(meta, activities) {
  const capabilities = meta?.capabilities ?? {}
  const years = meta?.data?.years
  if (!Array.isArray(years) || years.length === 0) fail('meta.json has no data.years')
  if (!Array.isArray(activities?.activities)) fail('activities.json has no activities list')
  const analyses = prewarmedAnalyses({
    years,
    presets: capabilities.activity_presets ?? [],
    measures: (capabilities.measures ?? []).map((measure) => measure.name),
    weightSchemes: (capabilities.weight_schemes ?? []).map((scheme) => scheme.name),
    tier1: activities.activities
      .filter((activity) => activity.level === 1)
      .map((activity) => ({ code: activity.code, name: activity.name })),
  })
  return analyses.map((analysis) =>
    entry(
      analysis.name,
      `${API_PREFIX}/analysis/${analysis.operation}`,
      stableStringify(analysis.spec),
    ),
  )
}

function canonicalPass(responses) {
  if (!Array.isArray(responses)) fail('responses.json must be a JSON array')
  const seen = new Set()
  const list = []
  for (const item of responses) {
    const spec = item?.response?.spec
    if (typeof item?.path !== 'string' || spec === null || typeof spec !== 'object') continue
    const request = entry(`canonical: ${item.path}`, item.path, stableStringify(spec))
    if (seen.has(request.key)) continue
    seen.add(request.key)
    list.push(request)
  }
  return list
}

function main() {
  const { values } = parseArgs({
    options: {
      meta: { type: 'string' },
      activities: { type: 'string' },
      canonical: { type: 'string' },
    },
  })
  let list
  if (values.canonical !== undefined) {
    list = canonicalPass(readJson(values.canonical))
  } else {
    if (values.meta === undefined || values.activities === undefined) {
      fail('usage: --meta meta.json --activities activities.json | --canonical responses.json')
    }
    list = prewarmPass(readJson(values.meta), readJson(values.activities))
  }
  process.stdout.write(`${JSON.stringify(list, null, 2)}\n`)
}

main()
