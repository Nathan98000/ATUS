/**
 * Reads the static site's `_headers` file (`public/_headers`, copied into
 * `dist/`), the response-header rules Cloudflare Workers static assets apply
 * to every static file (ADR-011). It is the single source of the SPA's
 * Content-Security-Policy and caching rules:
 *
 * - `vite preview` — and therefore every E2E run — applies the built copy
 *   with `headersForPath`, so the browser tests run under the deployed rules;
 * - the build derives the `<meta>` CSP backstop in `index.html` from it
 *   (`metaContentSecurityPolicy`);
 * - `scripts/check-bundle.mjs` checks the built copy before a deploy.
 *
 * Supported syntax, as documented for Workers static assets: a rule is an
 * unindented URL path pattern followed by indented `Name: value` lines;
 * `! Name` detaches a header set by another rule; `#` starts a comment line.
 * A pattern may hold one splat (`*`, greedy) and named placeholders
 * (`:name`, one path segment). A request matching several rules gets all of
 * their headers, and the same header set twice is joined with ", ". Anything
 * else (absolute URLs, malformed lines, more than 100 rules, lines over 2,000
 * characters) is rejected here rather than silently ignored at the edge.
 */

export interface HeaderRule {
  /** The URL path pattern, e.g. `/assets/*` or `/snap/:snapshot/*`. */
  pattern: string
  /** Headers to attach (lowercase names, in file order). */
  set: [name: string, value: string][]
  /** Headers to detach (lowercase names). */
  unset: string[]
  matcher: RegExp
}

const MAX_RULES = 100
const MAX_LINE_LENGTH = 2_000
const HEADER_NAME = /^[A-Za-z0-9!#$%&'*+.^_`|~-]+$/
const PLACEHOLDER = /:(\w+)/g

export class HeadersFileError extends Error {
  constructor(lineNumber: number, message: string) {
    super(`_headers line ${lineNumber}: ${message}`)
    this.name = 'HeadersFileError'
  }
}

function patternMatcher(pattern: string, lineNumber: number): RegExp {
  if ((pattern.match(/\*/g) ?? []).length > 1) {
    throw new HeadersFileError(lineNumber, `more than one splat in ${pattern}`)
  }
  const names = [...pattern.matchAll(PLACEHOLDER)].map((match) => match[1])
  if (new Set(names).size !== names.length) {
    throw new HeadersFileError(lineNumber, `a placeholder is used twice in ${pattern}`)
  }
  const source = pattern
    .replace(/[.+?^${}()|[\]\\]/g, '\\$&')
    .replace('*', '(?<splat>.*)')
    .replace(PLACEHOLDER, '(?<$1>[^/]+)')
  return new RegExp(`^${source}$`)
}

export function parseHeadersFile(text: string): HeaderRule[] {
  const rules: HeaderRule[] = []
  let current: HeaderRule | null = null
  text.split(/\r?\n/).forEach((rawLine, index) => {
    const lineNumber = index + 1
    if (rawLine.length > MAX_LINE_LENGTH) {
      throw new HeadersFileError(lineNumber, `longer than ${MAX_LINE_LENGTH} characters`)
    }
    const line = rawLine.trim()
    if (line === '' || line.startsWith('#')) return

    if (!/^\s/.test(rawLine)) {
      // An unindented line starts a rule.
      if (!line.startsWith('/') || /\s/.test(line)) {
        throw new HeadersFileError(lineNumber, `expected a URL path pattern, got "${line}"`)
      }
      current = { pattern: line, set: [], unset: [], matcher: patternMatcher(line, lineNumber) }
      rules.push(current)
      if (rules.length > MAX_RULES) {
        throw new HeadersFileError(lineNumber, `more than ${MAX_RULES} rules`)
      }
      return
    }

    if (current === null) throw new HeadersFileError(lineNumber, 'header before any rule')
    const rule: HeaderRule = current
    if (line.startsWith('!')) {
      const name = line.slice(1).trim()
      if (!HEADER_NAME.test(name))
        throw new HeadersFileError(lineNumber, `bad header "${name}"`)
      rule.unset.push(name.toLowerCase())
      return
    }
    const colon = line.indexOf(':')
    const name = colon > 0 ? line.slice(0, colon).trim() : ''
    const value = line.slice(colon + 1).trim()
    if (!HEADER_NAME.test(name) || value === '') {
      throw new HeadersFileError(lineNumber, `expected "Name: value", got "${line}"`)
    }
    rule.set.push([name.toLowerCase(), value])
  })
  return rules
}

/** The headers the rules attach to a static file at `pathname` (lowercase names). */
export function headersForPath(
  rules: readonly HeaderRule[],
  pathname: string,
): Map<string, string> {
  const headers = new Map<string, string>()
  for (const rule of rules) {
    if (!rule.matcher.test(pathname)) continue
    for (const name of rule.unset) headers.delete(name)
    for (const [name, value] of rule.set) {
      const previous = headers.get(name)
      headers.set(name, previous === undefined ? value : `${previous}, ${value}`)
    }
  }
  return headers
}

/**
 * The policy for a `<meta http-equiv>` CSP: the same directives minus those
 * a meta element cannot carry (browsers ignore them there and log an error).
 * `frame-ancestors` stays enforced by the response header.
 */
export function metaContentSecurityPolicy(policy: string): string {
  const unsupported = new Set(['frame-ancestors', 'report-uri', 'report-to', 'sandbox'])
  return policy
    .split(';')
    .map((directive) => directive.trim())
    .filter((directive) => directive !== '')
    .filter((directive) => !unsupported.has(directive.split(/\s+/)[0]?.toLowerCase() ?? ''))
    .join('; ')
}
