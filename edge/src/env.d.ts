// Bindings `wrangler types` cannot describe (worker-configuration.d.ts is
// generated from wrangler.jsonc, where optional secrets are not listed).

interface Env {
  /**
   * Optional project-scoped Neon API key (Worker secret). With it and the
   * `NEON_PROJECT_ID` var, the governor paces against Neon's own usage figures.
   */
  NEON_API_KEY?: string
  /**
   * The owner's pause switch (src/pause.ts), set and cleared by deploy.yml's
   * `pause-api` / `resume-api` operations as Worker secrets; never a var.
   */
  API_PAUSED_UNTIL?: string
  API_PAUSED_REASON?: string
}

declare namespace Cloudflare {
  interface Env {
    NEON_API_KEY?: string
    API_PAUSED_UNTIL?: string
    API_PAUSED_REASON?: string
  }
}
