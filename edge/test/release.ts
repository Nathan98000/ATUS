/**
 * The Worker vars the worker tests' fixtures are written for (origin headers,
 * KV metadata, cache scopes and version reports computed independently),
 * bound over edge/wrangler.jsonc's values by vitest.config.ts. The deploy sets
 * RESPONSE_FORMAT with --var; EPOCH, PREWAKE and NEON_PROJECT_ID are the
 * levers the owner changes in wrangler.jsonc by hand (docs/deployment.md,
 * "Cloudflare Worker"). Pinned here, such a change edits wrangler.jsonc alone
 * (a RESPONSE_FORMAT bump also test/node/config.test.ts), never these
 * fixtures: review round 2, response-format-bump-breaks-snapshot; review
 * round 4, epoch-prewake-levers-blocked-by-config-test (an EPOCH bump, the
 * documented edge-cache flush, failed 8 worker tests, PREWAKE=off 11).
 */
export const TEST_RELEASE = {
  RESPONSE_FORMAT: '1',
  EPOCH: '1',
  PREWAKE: 'on',
  NEON_PROJECT_ID: '',
}
