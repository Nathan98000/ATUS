/**
 * Audit F30: `npm run generate:api-types` reproduces the committed
 * src/api/generated.ts from openapi.json, and `npm run check:api-types`
 * reports any drift between the two.
 */
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'

const FRONTEND = process.cwd()
const COMMITTED = readFileSync(join(FRONTEND, 'src', 'api', 'generated.ts'), 'utf8')

const scratch: string[] = []
afterEach(() => {
  for (const dir of scratch.splice(0)) rmSync(dir, { recursive: true, force: true })
})

function tempDir(): string {
  const dir = mkdtempSync(join(tmpdir(), 'api-types-test-'))
  scratch.push(dir)
  return dir
}

function run(script: string, args: string[]) {
  return spawnSync(process.execPath, [script, ...args], { cwd: FRONTEND, encoding: 'utf8' })
}

describe('scripts/api-types.mjs', () => {
  it('--check passes on the committed generated.ts', () => {
    const result = run('scripts/api-types.mjs', ['--check'])
    expect(result.stderr).toBe('')
    expect(result.status).toBe(0)
    expect(result.stdout).toContain('matches openapi.json')
  }, 30_000)

  it('--write reproduces the committed file byte for byte', () => {
    const output = join(tempDir(), 'generated.ts')
    const result = run('scripts/api-types.mjs', ['--write', '--output', output])
    expect(result.status, result.stderr).toBe(0)
    expect(readFileSync(output, 'utf8')).toBe(COMMITTED)
  }, 30_000)

  it('--check fails, naming the fix, when the committed file has drifted', () => {
    const output = join(tempDir(), 'generated.ts')
    writeFileSync(output, COMMITTED.replace("'/api/v1/analysis/estimate'", "'/api/v1/x'"))
    const result = run('scripts/api-types.mjs', ['--check', '--output', output])
    expect(result.status).toBe(1)
    expect(result.stderr).toMatch(/first difference at line \d+/)
    expect(result.stderr).toContain('npm run generate:api-types')
  }, 30_000)

  it('keeps defaulted request properties optional (the flag F30 found missing)', () => {
    // Optional, as the API accepts them: the app sends partial specs.
    expect(COMMITTED).toMatch(/CompareRequest: {[\s\S]*?\n\s+measure\?:/)
    expect(COMMITTED).toMatch(/TrendRequest: {[\s\S]*?\n\s+weights\?:/)

    // The bare generator (the old npm script) makes them required instead.
    const output = join(tempDir(), 'bare.ts')
    const bare = run('node_modules/openapi-typescript/bin/cli.js', [
      'openapi.json',
      '-o',
      output,
    ])
    expect(bare.status, bare.stderr).toBe(0)
    const text = readFileSync(output, 'utf8')
    expect(text).toMatch(/CompareRequest: {[\s\S]*?\n\s+measure:/)
  }, 30_000)

  it('refuses to run without exactly one mode', () => {
    expect(run('scripts/api-types.mjs', []).status).toBe(2)
    expect(run('scripts/api-types.mjs', ['--write', '--check']).status).toBe(2)
  })
})
