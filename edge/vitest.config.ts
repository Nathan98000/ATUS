import { cloudflareTest } from '@cloudflare/vitest-plugin'
import { defineConfig } from 'vitest/config'
import { TEST_RELEASE } from './test/release'

// Fake values for the Worker's required secrets. RENDER_ORIGIN uses the
// reserved `.invalid` TLD, so an unmocked origin call can never leave the
// machine. Wrangler reads required secrets from process.env, so they are
// also forced there: a real secret exported in the developer's shell can
// never reach a test.
const TEST_SECRETS = {
  EDGE_AUTH_SECRET: 'test-edge-secret-0123456789abcdef',
  RENDER_ORIGIN: 'https://origin.invalid',
  OPS_TOKEN: 'test-ops-token',
  IP_HMAC_KEY: 'test-ip-hmac-key',
}
Object.assign(process.env, TEST_SECRETS)

// The worker tests pin the vars their fixtures are written for (TEST_RELEASE,
// test/release.ts) instead of inheriting wrangler.jsonc's values: the deploy's
// --var RESPONSE_FORMAT, and the levers the owner changes there by hand (EPOCH,
// PREWAKE, NEON_PROJECT_ID). Changing one then edits wrangler.jsonc, never these
// fixtures (review rounds 2 and 4; tests/unit/api/test_response_format.py and
// test/node/config.test.ts check the pin).

// Two projects, neither of which talks to Cloudflare or Render:
//
// - "worker": tests run inside workerd (the Workers runtime) through
//   Miniflare, with the real wrangler.jsonc; the origin is a mocked `fetch`
//   or the local HTTP server started by test/global-setup.ts.
// - "node": checks that need Node — the wrangler.jsonc invariants (read with
//   Wrangler's own parser) and scripts/ops/gen_edge_secrets.sh.
export default defineConfig({
  test: {
    projects: [
      {
        plugins: [
          cloudflareTest({
            wrangler: { configPath: './wrangler.jsonc' },
            miniflare: { bindings: { ...TEST_SECRETS, ...TEST_RELEASE } },
          }),
        ],
        test: {
          name: 'worker',
          include: ['test/worker/**/*.test.ts'],
          globalSetup: ['test/global-setup.ts'],
          restoreMocks: true,
        },
      },
      {
        test: {
          name: 'node',
          include: ['test/node/**/*.test.ts'],
          environment: 'node',
          restoreMocks: true,
        },
      },
    ],
  },
})
