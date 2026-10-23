# ADR-016: Short share links are deferred

**Status:** Accepted (2026-10-20) · Phase 5. The decision is to **not** build short links now.

**Related:** ADR-009 (shareable URLs), ADR-011 (hosting), ADR-012 (load and quota protection), ADR-015 (edge cache, snapshot and governor).

## Context

ADR-009 made the URL the analysis: `/analysis/<operation>?spec=<base64url(sorted-keys JSON)>`. Opening a link decodes it and reruns the analysis, so links need no server-side state. They are long, though. Measured on the encoding (character counts of the path and query, not timings):

| Link | Characters |
|---|---|
| "How much do Americans sleep?" starter, as linked from the home page | 134 |
| men vs women TV comparison starter | 255 |
| the same sleep estimate after the page rewrites it to the API's canonical spec | 326 |
| leisure trend 2003–2025, canonical | 508 |
| a broad selector (18 tier-1 codes, 22 years, 5 population filters) | 495 |

All of these are far below URL limits, and they paste fine into chat and email. ADR-009 noted that "if analyses ever need short URLs, a Phase 5 redirect service can map keys → specs". The brief (§14) asks for a decision and points out that short links need server-side state, which conflicts with the stateless $0 design.

A short link needs somewhere to store `id → spec` and something that resolves `id` at request time. Under ADR-011 the candidates are:

1. **Workers KV**, written by the edge Worker (`POST /api/v1/links`, then `GET /s/<id>` redirecting to the long link).
2. **The governor's Durable Object storage** (SQLite).
3. **A table in the Neon serving database.**
4. **A third-party shortener.**
5. **Stateless compression of the spec** (for example deflate-raw plus base64url in a new `?z=` parameter). This makes links shorter but not short.

## Decision

**Deferred.** ADR-009's long links stay the only share format. No link endpoint, no `/s/` route and no link storage are built in Phase 5.

### Why

Every stateful option takes something away from the $0 design that the long links get for free:

- **Availability.** A long link works whenever the static site does. The page, its metadata and about 130 prewarmed analyses are static files that never invoke the Worker (ADR-011, "Consequences"). A short link needs the Worker to resolve it. It would therefore be the first thing to stop working when the Worker's 100k requests/day are used up (until 00:00 UTC), and options 2 and 3 add further quotas. A shared link is exactly what a burst of visitors arrives with.
- **Writes are the scarcest quota.** Workers KV Free allows 1,000 writes per day. The persistent result cache (ADR-011, L1) already spends them: up to the governor's 600 uncached analyses per day. Link creation would compete with it, or need a budget of its own in the governor.
- **The database stays read-only.** Option 3 breaks the `atus_api` SELECT-only role (ADR-011, "Roles"). It would also wake Render and Neon to resolve a redirect, which is about a minute on a cold start, and spend CU-hours on it.
- **Permanence.** A link must outlive provider changes. The workers.dev hostname is the only fixed point (ADR-011), and the API host, database project and data release may all change behind it. A link store would be a second thing that every migration, restore and fallback swap must carry across, with its own backup. KV has no free point-in-time backup. Losing the namespace would break every short link ever shared, while long links cannot be lost.
- **Abuse and privacy.** An unauthenticated write endpoint stores visitor-supplied text (group labels are free text) under the site's hostname, indefinitely. That is a spam and phishing surface, and it would need rate limiting, size limits and a takedown process. The store would also be a record of what was shared and when. Today the service logs no specs at all (About page, ADR-014).
- **KV is eventually consistent.** A new link can 404 in another region for 60 s or more, so sharing a link immediately after creating it would sometimes fail.
- **Third-party shorteners (option 4)** put another company's hostname, tracking and link rot into every share link, against the no-third-party policy. Rejected outright.

The remaining option (5, compression) keeps the design stateless. Its gain on links of 130–510 characters is modest, and it would add a second link format that has to be supported forever. It is the first step if length ever becomes a real problem (below).

### What would change this decision

- **Evidence that length hurts.** For example, reports that links break in a channel people actually use (SMS length limits, email clients that wrap URLs, QR codes on posters). There is no analytics to show this, so it would come from issues and feedback.
- **First response: stateless compression.** A versioned `?z=` parameter (deflate-raw via the browser's `CompressionStream`, then base64url) decoded by the SPA, with `?spec=` still accepted. Before adopting it, measure the shortening on the canonical links above.
- **Only if links must be truly short:** a stateful service, built with:
  - content-addressed ids (a truncated hash of the canonical spec, so creation is idempotent and ids cannot be enumerated);
  - creation admitted by the governor with its own daily write budget;
  - no expiry, and the namespace exported with every release backup;
  - a static `/s/<id>` page that explains what happened when the Worker cannot resolve the id.

  That needs a storage tier with backups and a write quota separate from the result cache. No $0, no-card option in ADR-011's fact sheet offers both today.
- **A custom domain or a paid plan** would remove some of these constraints. Both are excluded by the hard constraints (no purchased domain, $0 forever).

## Consequences

- Share links remain long but self-contained. They reproduce the analysis with no server-side state, keep working during Worker, Render or Neon outages for every analysis the snapshot holds, and cannot be lost.
- Nothing new to operate, back up, rate-limit or moderate.
- ADR-009's mention of "a Phase 5 redirect service" is superseded by this ADR. The URL contract of ADR-009 is unchanged, so a later short-link scheme can be added on top without breaking existing links.
