/** scripts/check-bundle.mjs: the deploy-time guard against a baked-in development API (F24). */
import { spawnSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'

const scratch: string[] = []

function distWith(code: string): string {
  const dist = mkdtempSync(join(tmpdir(), 'check-bundle-'))
  scratch.push(dist)
  mkdirSync(join(dist, 'assets'))
  writeFileSync(join(dist, 'assets', 'index-abc123.js'), code)
  return dist
}

function check(dist: string) {
  return spawnSync(process.execPath, ['scripts/check-bundle.mjs', dist], {
    cwd: process.cwd(),
    encoding: 'utf8',
  })
}

afterEach(() => {
  for (const dir of scratch.splice(0)) rmSync(dir, { recursive: true, force: true })
})

describe('check-bundle.mjs', () => {
  it('fails on a development API address', () => {
    for (const address of [
      'http://localhost:8000',
      'http://127.0.0.1:8137',
      'http://[::1]:8000',
    ]) {
      const result = check(distWith(`fetch(${JSON.stringify(address)}+"/api/v1/meta")`))
      expect(result.status, address).toBe(1)
      expect(result.stderr).toContain('development API addresses')
    }
  })

  it("passes a same-origin bundle, including react-router's portless URL base", () => {
    const result = check(
      distWith('var a=`/api/v1`;var b=new URL(`http://localhost`);fetch(a+"/meta")'),
    )
    expect(result.status).toBe(0)
  })
})
