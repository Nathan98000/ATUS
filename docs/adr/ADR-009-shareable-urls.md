# ADR-009: Shareable analyses encode the canonical spec in the URL

**Status:** accepted (Phase 4)

## Context

A copied URL must reproduce the analysis — configuration *and* result — with
no server-side user state (Phase 3 is deliberately stateless: there is no
"saved analysis" resource to point at). The Phase 3 response already
contains the canonical `spec` (resubmittable verbatim) and the deterministic
`X-Analysis-Key` (SHA-256 of operation + canonical spec + versions).

## Decision

```text
/analysis/<operation>?spec=<base64url(UTF-8 JSON, keys sorted deeply)>
```

- The **request spec is the URL**. A key alone was rejected: the API cannot
  resolve a hash back into a request (it stores nothing), so a key-only URL
  could identify but never reconstruct an analysis. `X-Analysis-Key` is
  surfaced in the methodology panel as the reproducibility identifier.
- **Canonicalization follows the backend.** Sorted-keys serialization plus a
  post-run URL rewrite (history `replace`) to the response's own canonical
  spec make equivalent requests — reordered years/codes, expanded presets —
  converge on one link, exactly the equivalence the backend's cache key
  defines. The already-fetched result is seeded into the query cache under
  the canonical key, so the rewrite never refetches. Compare responses have
  per-group specs but no single top-level spec, so compare URLs keep the
  request encoding.
- base64url (RFC 4648 §5) of UTF-8 JSON: URL-safe without percent-escaping,
  handles any activity label (tested with non-ASCII), and keeps specs
  (~100–400 chars) far below URL limits. Not encryption and not meant to be:
  specs contain no secrets.
- The builder edits locally and commits **one** history entry per Analyze —
  no per-keystroke history pollution; Back returns to the previous analysis.
- Malformed or truncated links get structural validation and a dedicated
  "link cannot be opened" page with a path back to the builder.

## Consequences

- Deep links, refresh, and bookmarks work with zero backend additions.
- URLs are long-ish but stable and canonical; if analyses ever need short
  URLs, a Phase 5 redirect service can map keys → specs without changing
  this contract.
- If the data or analytics version changes, an old link re-runs the same
  spec against the new version — by design (the result page shows current
  versions in its methodology panel).
