/**
 * scripts/ops/gen_edge_secrets.sh: 256-bit secrets in a 0600 file, never
 * printed, idempotent re-runs, explicit rotation, and only `wrangler secret
 * put` commands that read from the file. Every run uses a throwaway HOME.
 */

import { spawnSync } from 'node:child_process'
import {
  chmodSync,
  lstatSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

const SCRIPT = fileURLToPath(
  new URL('../../../scripts/ops/gen_edge_secrets.sh', import.meta.url),
)
const GENERATED = ['EDGE_AUTH_SECRET', 'OPS_TOKEN', 'IP_HMAC_KEY'] as const

let home: string
let envFile: string

beforeEach(() => {
  home = mkdtempSync(join(tmpdir(), 'atus-edge-secrets-'))
  envFile = join(home, '.config', 'atus', 'edge.env')
})

afterEach(() => {
  rmSync(home, { recursive: true, force: true })
})

function run(...args: string[]): { status: number | null; stdout: string; stderr: string } {
  const result = spawnSync('bash', [SCRIPT, ...args], {
    env: { PATH: process.env.PATH ?? '/usr/bin:/bin', HOME: home },
    encoding: 'utf8',
  })
  return { status: result.status, stdout: result.stdout, stderr: result.stderr }
}

function readValues(path = envFile): Record<string, string> {
  const values: Record<string, string> = {}
  for (const line of readFileSync(path, 'utf8').split('\n')) {
    const match = /^([A-Z_]+)=(.*)$/.exec(line)
    if (match) values[match[1]!] = match[2]!
  }
  return values
}

function mode(path: string): number {
  return lstatSync(path).mode & 0o777
}

describe('gen_edge_secrets.sh', () => {
  it('writes three 256-bit hex secrets to ~/.config/atus/edge.env with 0600/0700 modes', () => {
    const { status, stdout, stderr } = run()
    expect(status, stderr).toBe(0)
    const values = readValues()
    for (const name of GENERATED) expect(values[name], name).toMatch(/^[0-9a-f]{64}$/)
    expect(new Set(GENERATED.map((name) => values[name])).size).toBe(3)
    expect(values).not.toHaveProperty('RENDER_ORIGIN')
    expect(mode(envFile)).toBe(0o600)
    expect(mode(join(home, '.config', 'atus'))).toBe(0o700)
    expect(stdout).toContain('EDGE_AUTH_SECRET: created')
  })

  it('never prints a value, and prints only secret-put commands that read the file', () => {
    const { stdout, stderr } = run('--render-origin', 'https://atus-api-abc123.onrender.com')
    const values = readValues()
    for (const value of Object.values(values)) {
      expect(stdout).not.toContain(value)
      expect(stderr).not.toContain(value)
    }
    const commands = stdout.split('\n').filter((line) => line.includes('wrangler'))
    expect(commands).toHaveLength(4)
    for (const name of [...GENERATED, 'RENDER_ORIGIN']) {
      expect(commands).toContain(
        `  grep '^${name}=' ${envFile} | cut -d= -f2- | npx wrangler secret put ${name}`,
      )
    }
    expect(values.RENDER_ORIGIN).toBe('https://atus-api-abc123.onrender.com')
  })

  it('keeps existing values on a re-run, and other lines in the file', () => {
    run()
    const first = readValues()
    writeFileSync(envFile, readFileSync(envFile, 'utf8') + 'NEON_API_KEY=owner-added\n')
    const { stdout } = run()
    const second = readValues()
    for (const name of GENERATED) expect(second[name]).toBe(first[name])
    expect(second.NEON_API_KEY).toBe('owner-added')
    expect(stdout).toContain('OPS_TOKEN: kept')
  })

  it('rotates only the named secret and keeps the previous edge secret for the switch', () => {
    run()
    const before = readValues()
    const { stdout } = run('--rotate', 'EDGE_AUTH_SECRET')
    const after = readValues()
    expect(after.EDGE_AUTH_SECRET).toMatch(/^[0-9a-f]{64}$/)
    expect(after.EDGE_AUTH_SECRET).not.toBe(before.EDGE_AUTH_SECRET)
    expect(after.EDGE_AUTH_SECRET_PREVIOUS).toBe(before.EDGE_AUTH_SECRET)
    expect(after.OPS_TOKEN).toBe(before.OPS_TOKEN)
    expect(after.IP_HMAC_KEY).toBe(before.IP_HMAC_KEY)
    expect(stdout).toContain('EDGE_AUTH_SECRET: rotated')
    expect(stdout).toContain('Rotation order')
    expect(stdout).not.toContain(after.EDGE_AUTH_SECRET_PREVIOUS!)
    // The previous value is never offered for loading into the Worker.
    expect(stdout).not.toContain('secret put EDGE_AUTH_SECRET_PREVIOUS')
  })

  it('rotates everything with --rotate all', () => {
    run()
    const before = readValues()
    run('--rotate', 'all')
    const after = readValues()
    for (const name of GENERATED) expect(after[name]).not.toBe(before[name])
  })

  it('re-tightens a loosened file to 0600', () => {
    run()
    chmodSync(envFile, 0o644)
    run()
    expect(mode(envFile)).toBe(0o600)
  })

  it('honours --file', () => {
    const custom = join(home, 'elsewhere', 'edge.env')
    const { status, stdout } = run('--file', custom)
    expect(status).toBe(0)
    expect(Object.keys(readValues(custom))).toEqual([...GENERATED])
    expect(stdout).toContain(`grep '^OPS_TOKEN=' ${custom}`)
  })

  it.each([
    ['plain http', 'http://atus-api.onrender.com'],
    ['a path', 'https://atus-api.onrender.com/api'],
    ['credentials', 'https://user:pw@atus-api.onrender.com'],
    ['a query', 'https://atus-api.onrender.com?x=1'],
    ['not a URL', 'atus-api.onrender.com'],
  ])('rejects a RENDER_ORIGIN with %s and writes nothing', (_, origin) => {
    const { status, stderr } = run('--render-origin', origin)
    expect(status).not.toBe(0)
    expect(stderr).toContain('--render-origin')
    expect(() => lstatSync(envFile)).toThrow()
  })

  it('accepts a trailing slash on RENDER_ORIGIN and stores the bare origin', () => {
    run('--render-origin', 'https://atus-api-abc123.onrender.com/')
    expect(readValues().RENDER_ORIGIN).toBe('https://atus-api-abc123.onrender.com')
  })

  it('refuses to write through a symlink', () => {
    run()
    const target = join(home, 'target.env')
    writeFileSync(target, '')
    rmSync(envFile)
    symlinkSync(target, envFile)
    const { status, stderr } = run()
    expect(status).not.toBe(0)
    expect(stderr).toContain('symlink')
    expect(readFileSync(target, 'utf8')).toBe('')
  })

  it('rejects unknown arguments and rotation names', () => {
    expect(run('--bogus').status).not.toBe(0)
    expect(run('--rotate', 'RENDER_ORIGIN').status).not.toBe(0)
  })
})
