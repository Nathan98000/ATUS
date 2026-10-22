import { useEffect, useRef, type ReactNode, type RefObject } from 'react'
import { Link, NavLink, NavigationType, useLocation, useNavigationType } from 'react-router'

import { useMeta } from '../api/queries'
import { ServiceNotice } from './ServiceNotice'
import { NavigationFocus, PAGE_HEADING } from './useRetryFocus'

/**
 * After an in-app navigation, focus moves to the new page's heading, so
 * keyboard and screen-reader users learn that the page changed and the next
 * Tab continues from it instead of from the top of the document (review
 * round 2). Not on the first page of a visit (the browser's own load), and
 * not when only the query string is replaced: that is the analysis page
 * rewriting its URL to the canonical spec, not a new page.
 *
 * Returns whether a navigation has moved focus this visit, for
 * `NavigationFocus`: a full-page state that then replaces the focused
 * heading moves focus to its own (review round 4).
 */
function useFocusOnNavigation(): RefObject<boolean> {
  const { pathname, search } = useLocation()
  const navigationType = useNavigationType()
  const shown = useRef({ pathname, search })
  const navigated = useRef(false)

  useEffect(() => {
    const before = shown.current
    shown.current = { pathname, search }
    const newPage =
      pathname !== before.pathname ||
      (search !== before.search && navigationType !== NavigationType.Replace)
    if (newPage) {
      navigated.current = true
      document.querySelector<HTMLElement>(PAGE_HEADING)?.focus()
    }
  }, [pathname, search, navigationType])
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
 * footer's release line appears once metadata has loaded; `/meta` arrives
 * already checked for compatibility, so the shell never renders a shape it
 * cannot read (audit F25).
 */
export function AppShell({ children }: { children: ReactNode }) {
  const meta = useMeta()
  const navigated = useFocusOnNavigation()

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
            <NavLink to="/about">About &amp; methodology</NavLink>
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
            Data source: U.S. Bureau of Labor Statistics,{' '}
            <a href="https://www.bls.gov/tus/" rel="noreferrer">
              American Time Use Survey
            </a>
            . Estimates are survey-weighted and subject to sampling uncertainty.
          </p>
          <p>
            An independent analytical project — not an official BLS product.
            {meta.data ? (
              <>
                {' '}
                Data release {meta.data.data.release} ({meta.data.data.years[0]}–
                {meta.data.data.years[meta.data.data.years.length - 1]}) · analytics{' '}
                {meta.data.analytics_version} · API {meta.data.api_version}
              </>
            ) : null}
          </p>
        </div>
      </footer>
    </>
  )
}
