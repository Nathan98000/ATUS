/**
 * File names of the built bundles (vite.config.ts, `build.rollupOptions.output`).
 *
 * `/assets/*` is cached as immutable for a year (public/_headers), and the
 * names are content hashes of the public source, so anyone can compute the
 * names the next build will have. A Worker version deployed with a leaked
 * Cloudflare token may have served its own files under them, and browsers
 * keep such copies. The recovery (docs/deployment.md "Rotation", item 5)
 * therefore commits a fresh random `ASSET_SALT`, which every bundle's name
 * then carries, so the recovered site asks for no bundle under a name a
 * browser may hold an attacker's copy of (`/boot.css` and `/favicon.svg` keep
 * theirs: docs/security.md R16). Empty: Vite's default names.
 */

/** Set by the recovery to `openssl rand -hex 8`; lowercase hex, at most 32 characters. */
export const ASSET_SALT = ''

export interface OutputFileNames {
  entryFileNames: string
  chunkFileNames: string
  assetFileNames: string
}

/** Vite's default bundle names, with `-<salt>` after the hash when `salt` is set. */
export function outputFileNames(salt: string): OutputFileNames {
  if (!/^[0-9a-f]{0,32}$/.test(salt)) {
    throw new Error(
      `ASSET_SALT must be lowercase hex of at most 32 characters, not ${JSON.stringify(salt)}`,
    )
  }
  const suffix = salt === '' ? '' : `-${salt}`
  return {
    entryFileNames: `assets/[name]-[hash]${suffix}.js`,
    chunkFileNames: `assets/[name]-[hash]${suffix}.js`,
    assetFileNames: `assets/[name]-[hash]${suffix}[extname]`,
  }
}
