#!/usr/bin/env node
/**
 * Generates `src/api/generated.ts` from the API's OpenAPI schema
 * (`openapi.json`, written by scripts/export_openapi.py), or checks that the
 * committed file is exactly what generation produces (audit F30).
 *
 *   node scripts/api-types.mjs --write    # npm run generate:api-types
 *   node scripts/api-types.mjs --check    # npm run check:api-types (exit 1 on drift)
 *
 * `--openapi <file>` and `--output <file>` override the two paths.
 *
 * The generator runs with `--default-non-nullable false`: a request property
 * that has a server-side default (measure, weights, variance, …) stays
 * optional in the request types, as the API accepts it. Without the flag
 * those properties become required and the app no longer type-checks — the
 * defect F30 found in the previous one-line npm script.
 */
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parseArgs } from 'node:util'

import * as prettier from 'prettier'

const OPENAPI_TYPESCRIPT_FLAGS = ['--default-non-nullable', 'false']

const FRONTEND = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const GENERATED = join(FRONTEND, 'src', 'api', 'generated.ts')
const require = createRequire(import.meta.url)

function fail(message, code = 2) {
  process.stderr.write(`api-types: ${message}\n`)
  process.exit(code)
}

/** The generated module, formatted with the project's Prettier settings. */
async function generate(openapiPath, outputPath) {
  const cli = join(dirname(require.resolve('openapi-typescript/package.json')), 'bin', 'cli.js')
  const scratch = mkdtempSync(join(tmpdir(), 'api-types-'))
  try {
    const raw = join(scratch, 'generated.ts')
    const result = spawnSync(
      process.execPath,
      [cli, openapiPath, '-o', raw, ...OPENAPI_TYPESCRIPT_FLAGS],
      { encoding: 'utf8' },
    )
    if (result.status !== 0) fail(`openapi-typescript failed:\n${result.stderr}`)
    // The project's Prettier settings, wherever the output goes.
    const options = (await prettier.resolveConfig(GENERATED)) ?? {}
    return await prettier.format(readFileSync(raw, 'utf8'), {
      ...options,
      filepath: outputPath,
    })
  } finally {
    rmSync(scratch, { recursive: true, force: true })
  }
}

const { values } = parseArgs({
  options: {
    write: { type: 'boolean', default: false },
    check: { type: 'boolean', default: false },
    openapi: { type: 'string', default: join(FRONTEND, 'openapi.json') },
    output: { type: 'string', default: GENERATED },
  },
})
if (values.write === values.check) fail('pass exactly one of --write or --check')

const generated = await generate(resolve(values.openapi), resolve(values.output))
const shown = (path) => relative(process.cwd(), resolve(path)) || path

if (values.write) {
  writeFileSync(values.output, generated)
  process.stdout.write(`api-types: wrote ${shown(values.output)}\n`)
} else {
  let committed = ''
  try {
    committed = readFileSync(values.output, 'utf8')
  } catch (error) {
    fail(`cannot read ${values.output}: ${error.message}`)
  }
  if (committed !== generated) {
    const ours = committed.split('\n')
    const theirs = generated.split('\n')
    let line = 0
    while (line < ours.length && ours[line] === theirs[line]) line += 1
    fail(
      `${shown(values.output)} is not what ${shown(values.openapi)} generates ` +
        `(first difference at line ${line + 1}). ` +
        'Run `npm run generate:api-types` and commit the result.',
      1,
    )
  }
  process.stdout.write(`api-types: ${shown(values.output)} matches ${shown(values.openapi)}\n`)
}
