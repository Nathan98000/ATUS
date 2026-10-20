/**
 * scripts/snapshot-requests.mjs, run as the snapshot builder runs it (Node),
 * must print exactly the bytes and keys the SPA computes in the browser.
 */
import { execFileSync, spawnSync } from 'node:child_process'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterAll, describe, expect, it } from 'vitest'

import { API_VERSION_PREFIX } from '../api/client'
import { requestKey } from '../api/snapshot'
import { compareChildrenFixture, estimateSleepFixture } from '../test/fixtures'
import { stableStringify } from '../utils/stableStringify'
import { STARTER_EXAMPLES } from './prewarm'
import { analysisPath, decodeSpec } from './urlSpec'

// Vitest runs from the frontend directory (npm test).
const FRONTEND = process.cwd()
const FIXTURES = join(FRONTEND, 'src/test/fixtures')
const scratch = mkdtempSync(join(tmpdir(), 'snapshot-requests-'))

interface PrintedRequest {
  name: string
  path: string
  body: string
  key: string
}

function runScript(args: string[]): PrintedRequest[] {
  const output = execFileSync(process.execPath, ['scripts/snapshot-requests.mjs', ...args], {
    cwd: FRONTEND,
    encoding: 'utf8',
  })
  return JSON.parse(output) as PrintedRequest[]
}

afterAll(() => {
  rmSync(scratch, { recursive: true, force: true })
})

describe('snapshot-requests.mjs', () => {
  const printed = runScript([
    '--meta',
    join(FIXTURES, 'meta.json'),
    '--activities',
    join(FIXTURES, 'activities.json'),
  ])

  it('prints ~130 requests whose keys equal the SPA’s WebCrypto keys', async () => {
    expect(printed).toHaveLength(130)
    for (const request of printed) {
      expect(request.path).toMatch(
        new RegExp(`^${API_VERSION_PREFIX}/analysis/(estimate|trend|compare)$`),
      )
      expect(request.key).toBe(await requestKey(request.path, '', request.body))
      // Bodies are already in stable form: re-serializing changes nothing.
      expect(stableStringify(JSON.parse(request.body))).toBe(request.body)
    }
  })

  it('a starter link from the home page posts exactly the printed bytes', () => {
    const latest = 2025
    const years = Array.from({ length: 23 }, (_, index) => 2003 + index)
    STARTER_EXAMPLES.forEach((starter, index) => {
      const link = analysisPath(starter.operation, starter.spec(years, latest))
      const encoded = new URL(link, 'http://example.test').searchParams.get('spec') as string
      // What AnalysisPage decodes from the link and runAnalysis serializes:
      const sent = stableStringify(decodeSpec(encoded, starter.operation))
      expect(printed[index]?.body).toBe(sent)
      expect(printed[index]?.path).toBe(`${API_VERSION_PREFIX}/analysis/${starter.operation}`)
    })
  })

  it('the canonical pass prints the canonical spec of each estimate/trend response', () => {
    const responses = join(scratch, 'responses.json')
    writeFileSync(
      responses,
      JSON.stringify([
        { path: '/api/v1/analysis/estimate', response: estimateSleepFixture },
        { path: '/api/v1/analysis/estimate', response: estimateSleepFixture },
        { path: '/api/v1/analysis/compare', response: compareChildrenFixture },
      ]),
    )
    const canonical = runScript(['--canonical', responses])
    expect(canonical).toHaveLength(1) // duplicates dropped; compare has no single spec
    expect(canonical[0]?.body).toBe(stableStringify(estimateSleepFixture.spec))
  })

  it('fails loudly without its inputs', () => {
    const result = spawnSync(process.execPath, ['scripts/snapshot-requests.mjs'], {
      cwd: FRONTEND,
      encoding: 'utf8',
    })
    expect(result.status).toBe(2)
    expect(result.stderr).toMatch(/usage/)
  })
})
