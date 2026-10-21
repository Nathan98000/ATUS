/**
 * scripts/ops/gen_edge_secrets.sh: 256-bit secrets in a 0600 file, never
 * printed, idempotent re-runs, explicit rotation, and only `wrangler secret
 * put` / `gh secret set` commands that read from the file. The API's
 * ATUS_OPS_TOKEN is generated into the same file but never offered to the
 * Worker or to GitHub. Every run uses a throwaway HOME.
 */

import { spawnSync } from 'node:child_process'
import {
  chmodSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

const SCRIPT = fileURLToPath(
  new URL('../../../scripts/ops/gen_edge_secrets.sh', import.meta.url),
)
const GENERATED = ['EDGE_AUTH_SECRET', 'OPS_TOKEN', 'IP_HMAC_KEY', 'ATUS_OPS_TOKEN'] as const
const WORKER_SECRETS = ['EDGE_AUTH_SECRET', 'OPS_TOKEN', 'IP_HMAC_KEY'] as const

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
  it('writes four 256-bit hex secrets to ~/.config/atus/edge.env with 0600/0700 modes', () => {
    const { status, stdout, stderr } = run()
    expect(status, stderr).toBe(0)
    const values = readValues()
    for (const name of GENERATED) expect(values[name], name).toMatch(/^[0-9a-f]{64}$/)
    expect(new Set(GENERATED.map((name) => values[name])).size).toBe(4)
    expect(values).not.toHaveProperty('RENDER_ORIGIN')
    expect(mode(envFile)).toBe(0o600)
    expect(mode(join(home, '.config', 'atus'))).toBe(0o700)
    expect(stdout).toContain('EDGE_AUTH_SECRET: created')
    expect(stdout).toContain('needs RENDER_ORIGIN first')
  })

  it('never prints a value, and prints only secret-put commands that read the file', () => {
    const { stdout, stderr } = run('--render-origin', 'https://atus-api-abc123.onrender.com')
    const values = readValues()
    for (const value of Object.values(values)) {
      expect(stdout).not.toContain(value)
      expect(stderr).not.toContain(value)
    }
    const commands = stdout.split('\n').filter((line) => line.includes('wrangler'))
    expect(commands).toHaveLength(5)
    for (const name of [...WORKER_SECRETS, 'RENDER_ORIGIN']) {
      expect(commands).toContain(
        `  grep '^${name}=' ${envFile} | cut -d= -f2- | npx --no-install wrangler secret put ${name}`,
      )
    }
    // The first deploy uploads the Worker's names only (--secrets-file sends every line).
    expect(commands).toContain(
      `  (umask 077 && tmp=$(mktemp -d) && trap 'rm -rf -- "$tmp"' EXIT && grep -E '^(EDGE_AUTH_SECRET|OPS_TOKEN|IP_HMAC_KEY|RENDER_ORIGIN)=' ${envFile} > "$tmp/worker.env" && npx --no-install wrangler deploy --secrets-file "$tmp/worker.env")`,
    )
    expect(stdout).not.toContain('needs RENDER_ORIGIN first')
    expect(values.RENDER_ORIGIN).toBe('https://atus-api-abc123.onrender.com')
  })

  it("prints a first-deploy command that works: wrangler's CLI process reads the Worker's names only, and the copy is removed", () => {
    // Regression: the command was `npx wrangler deploy --secrets-file <(grep ...)`.
    // npx and wrangler's own launcher start the CLI as child processes, and on
    // macOS Node opens only stdin/stdout/stderr in a child, so /dev/fd/N was
    // gone (EBADF) by the time wrangler read it. This stand-in for npx starts
    // a child process exactly like wrangler's launcher (bin/wrangler.js) and
    // reports what that child could read.
    const { stdout } = run('--render-origin', 'https://atus-api-abc123.onrender.com')
    const command = stdout.split('\n').find((line) => line.includes('wrangler deploy'))
    expect(command).toBeDefined()
    writeFileSync(envFile, readFileSync(envFile, 'utf8') + 'NEON_API_KEY=owner-added\n')

    const bin = join(home, 'bin')
    mkdirSync(bin)
    const launcher = join(bin, 'fake-wrangler.cjs')
    writeFileSync(
      launcher,
      `const { spawn } = require('node:child_process')
if (process.argv[2] === 'child') {
  const args = process.argv.slice(3)
  const path = args[args.indexOf('--secrets-file') + 1]
  const names = require('node:fs').readFileSync(path, 'utf8').split('\\n')
    .filter(Boolean).map((line) => line.split('=')[0])
  process.stdout.write(JSON.stringify({ args, path, names }))
} else {
  const child = spawn(process.execPath, [__filename, 'child', ...process.argv.slice(2)],
    { stdio: ['inherit', 'inherit', 'inherit', 'ipc'] })
  child.on('exit', (code) => process.exit(code ?? 1))
}
`,
    )
    const npx = join(bin, 'npx')
    writeFileSync(npx, `#!/bin/sh\nexec "${process.execPath}" "${launcher}" "$@"\n`)
    chmodSync(npx, 0o755)

    const result = spawnSync('bash', ['-c', command!], {
      env: { PATH: `${bin}:${process.env.PATH ?? '/usr/bin:/bin'}`, HOME: home },
      encoding: 'utf8',
    })
    expect(result.status, result.stderr).toBe(0)
    const seen = JSON.parse(result.stdout) as { args: string[]; path: string; names: string[] }
    // --no-install: npx runs the wrangler edge/'s lockfile installed, never a download
    expect(seen.args.slice(0, 3)).toEqual(['--no-install', 'wrangler', 'deploy'])
    expect(seen.names).toEqual([...WORKER_SECRETS, 'RENDER_ORIGIN'])
    expect(() => lstatSync(dirname(seen.path))).toThrow() // the private copy is gone
  })

  it('prints what a fresh clone needs before the first deploy: the SPA build and the locked tools', () => {
    // Regression: wrangler.jsonc publishes ../frontend/dist, which is
    // gitignored, so the printed first deploy failed on a fresh clone (or
    // published a stale local build); without `npm ci` in edge/, npx fetched
    // an unlocked wrangler.
    const { stdout } = run('--render-origin', 'https://atus-api-abc123.onrender.com')
    const lines = stdout.split('\n')
    const frontendDir = fileURLToPath(new URL('../../../frontend', import.meta.url))
    const edgeDir = fileURLToPath(new URL('../..', import.meta.url)).replace(/\/$/, '')
    const deploy = lines.findIndex((line) => line.includes('npx --no-install wrangler deploy'))
    const build = lines.indexOf(
      `  (cd ${frontendDir} && npm ci && VITE_API_BASE_URL= npm run build)`,
    )
    const install = lines.indexOf(`  cd ${edgeDir} && npm ci`)
    expect(build, stdout).toBeGreaterThanOrEqual(0)
    expect(install, stdout).toBeGreaterThan(build)
    expect(deploy).toBeGreaterThan(install)
  })

  it('generates the API ops token for Render only, never for the Worker or GitHub', () => {
    const { stdout } = run()
    expect(stdout).toContain('ATUS_OPS_TOKEN: created')
    expect(stdout).not.toContain('secret put ATUS_OPS_TOKEN')
    // Only the GitHub secret the workflows read (.github/workflows/quota-report.yml,
    // deploy.yml): EDGE_OPS_TOKEN, in `ops` and optionally `production`.
    const gh = stdout.split('\n').filter((line) => line.includes('gh secret set'))
    expect(gh).toEqual([
      `    grep '^OPS_TOKEN=' ${envFile} | cut -d= -f2- | gh secret set EDGE_OPS_TOKEN --env ops`,
      `    grep '^OPS_TOKEN=' ${envFile} | cut -d= -f2- | gh secret set EDGE_OPS_TOKEN --env production`,
    ])
    expect(stdout).toContain('Render -> Environment: ATUS_OPS_TOKEN')
    const values = readValues()
    expect(values.ATUS_OPS_TOKEN).not.toBe(values.OPS_TOKEN)
  })

  it('rotates the API ops token alone, and says where the new one goes', () => {
    run()
    const before = readValues()
    const { stdout } = run('--rotate', 'ATUS_OPS_TOKEN')
    const after = readValues()
    expect(after.ATUS_OPS_TOKEN).toMatch(/^[0-9a-f]{64}$/)
    expect(after.ATUS_OPS_TOKEN).not.toBe(before.ATUS_OPS_TOKEN)
    for (const name of WORKER_SECRETS) expect(after[name]).toBe(before[name])
    expect(stdout).toContain('ATUS_OPS_TOKEN: rotated')
    expect(stdout).toContain('Set the new ATUS_OPS_TOKEN on Render')
    // OPS_TOKEN was not rotated: its hint must not appear.
    expect(stdout).not.toContain('Also update every copy of OPS_TOKEN')
  })

  it('prints rotation hints only for values it actually replaced', () => {
    const { stdout } = run('--rotate', 'all')
    expect(stdout).toContain('EDGE_AUTH_SECRET: created')
    expect(stdout).not.toContain('Rotation order')
    expect(stdout).not.toContain('Set the new ATUS_OPS_TOKEN')
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
    expect(after.ATUS_OPS_TOKEN).toBe(before.ATUS_OPS_TOKEN)
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
    expect(run('--rotate', 'EDGE_AUTH_SECRET_PREVIOUS').status).not.toBe(0)
  })
})
