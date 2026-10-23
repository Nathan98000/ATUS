import { useEffect, useRef, type ReactNode, type RefObject } from 'react'
import { Link, NavLink, NavigationType, useLocation, useNavigationType } from 'react-router'

import { ServiceNotice } from './ServiceNotice'
import { NavigationFocus, PAGE_HEADING } from './useRetryFocus'

/**
 * After an in-app navigation, focus moves to the new page's heading, so
 * keyboard and screen-reader users learn that the page changed and the next
 * Tab continues from it instead of from the top of the document (review
 * round 2). Not on the first page of a visit (the browser's own load), and
 * not when only the query string is replaced: that is the analysis page
 * rewriting its URL to the canonical spec, not a new page. A link to a part
 * of a page ("/about#how-sure", from a result's "What does 95% mean?") moves
 * focus, and so the view, to that part instead — on the first page of a
 * visit too, since the browser cannot scroll to a part not yet drawn
 * (review round 6).
 *
 * Returns whether a navigation has moved focus this visit, for
 * `NavigationFocus`: a full-page state that then replaces the focused
 * heading moves focus to its own (review round 4).
 */
function useFocusOnNavigation(): RefObject<boolean> {
  const { pathname, search, hash } = useLocation()
  const navigationType = useNavigationType()
  // No part of a page is shown yet: a first visit to "#how-sure" goes there.
  const shown = useRef({ pathname, search, hash: '' })
  const navigated = useRef(false)

  useEffect(() => {
    const before = shown.current
    shown.current = { pathname, search, hash }
    const newPage =
      pathname !== before.pathname ||
      (search !== before.search && navigationType !== NavigationType.Replace)
    if (newPage) navigated.current = true
    const part =
      hash.length > 1 && (newPage || hash !== before.hash)
        ? document.getElementById(decodeURIComponent(hash.slice(1)))
        : null
    if (part !== null) part.focus()
    else if (newPage) document.querySelector<HTMLElement>(PAGE_HEADING)?.focus()
  }, [pathname, search, hash, navigationType])
  return navigated
}

/**
 * The site's mark: a clock face with one slice of the day filled in — what
 * every estimate here measures. The favicon is the same drawing.
 */
function ClockMark() {
  return (
    <svg className="wordmark__mark" viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <circle cx="16" cy="16" r="16" className="wordmark__dial" />
      <circle cx="16" cy="16" r="11" className="wordmark__ring" />
      <path d="M16 16 L16 7.5 A8.5 8.5 0 0 1 23.36 20.25 Z" className="wordmark__slice" />
    </svg>
  )
}

/**
 * Header, footer and the site-wide service notices around every page. The
 * data release and versions are on the About page, not in the footer
 * (review round 4: "analytics 0.2 · API v1" on every page meant nothing to
 * a reader).
 */
export function AppShell({ children }: { children: ReactNode }) {
  const navigated = useFocusOnNavigation()
  const { pathname } = useLocation()

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="site-header">
        <div className="site-header__inner">
          <Link to="/" className="wordmark">
            <ClockMark />
            ATUS Explorer
          </Link>
          <nav className="site-nav" aria-label="Main">
            <NavLink to="/explore">Explore</NavLink>
            <NavLink to="/about">
              About<span className="site-nav__more"> &amp; methodology</span>
            </NavLink>
          </nav>
        </div>
      </header>
      <main id="main">
        <ServiceNotice />
        <NavigationFocus value={navigated}>{children}</NavigationFocus>
      </main>
      <footer className="site-footer">
        <div className="site-footer__inner">
          <p>
            An independent project — not an official BLS product.
            {/* Not on the About page itself, which would link to itself. */}
            {pathname === '/about' ? null : (
              <>
                {' '}
                The data release and versions are on the{' '}
                <Link to="/about#data">About page</Link>.
              </>
            )}
          </p>
        </div>
      </footer>
    </>
  )
}
