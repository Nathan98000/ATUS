/**
 * The bundles' file names (src/deploy/assetNames.ts). `/assets/*` is cached
 * as immutable for a year, and the names are content hashes of the public
 * source, so a Worker version deployed with a leaked Cloudflare token could
 * serve its own files under the very names the recovery build produces;
 * browsers would keep those copies. The recovery sets a fresh `ASSET_SALT`
 * (docs/deployment.md "Rotation", item 5), which every name then carries
 * (review round 4, cf-compromise-recovery-leaves-planted-state).
 */
import { describe, expect, it } from 'vitest'

import { ASSET_SALT, outputFileNames } from './assetNames'

describe('outputFileNames', () => {
  it("keeps Vite's default names without a salt", () => {
    expect(outputFileNames('')).toEqual({
      entryFileNames: 'assets/[name]-[hash].js',
      chunkFileNames: 'assets/[name]-[hash].js',
      assetFileNames: 'assets/[name]-[hash][extname]',
    })
  })

  it('puts the salt in the name of every entry, chunk and asset', () => {
    const names = outputFileNames('0f3a9c51d2e4b687')
    expect(names).toEqual({
      entryFileNames: 'assets/[name]-[hash]-0f3a9c51d2e4b687.js',
      chunkFileNames: 'assets/[name]-[hash]-0f3a9c51d2e4b687.js',
      assetFileNames: 'assets/[name]-[hash]-0f3a9c51d2e4b687[extname]',
    })
    // Still under /assets/, which public/_headers caches as immutable.
    for (const pattern of Object.values(names)) expect(pattern).toMatch(/^assets\/[^/]+$/)
  })

  it('refuses a salt that is not lowercase hex, at most 32 characters', () => {
    for (const salt of ['0F3A', 'x/y', '[hash]', 'a'.repeat(33), ' 0f3a']) {
      expect(() => outputFileNames(salt), salt).toThrow(/ASSET_SALT/)
    }
  })

  it('is what the build uses, with the committed ASSET_SALT', async () => {
    expect(() => outputFileNames(ASSET_SALT)).not.toThrow()
    const { default: factory } = await import('../../vite.config')
    const config = (
      factory as unknown as (env: object) => {
        build?: { rollupOptions?: { output?: unknown } }
      }
    )({ command: 'build', mode: 'production', isSsrBuild: false, isPreview: false })
    expect(config.build?.rollupOptions?.output).toEqual(outputFileNames(ASSET_SALT))
  })
})
