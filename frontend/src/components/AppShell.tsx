import type { ReactNode } from 'react'
import { Link, NavLink } from 'react-router'

import { useMeta } from '../api/queries'

export function AppShell({ children }: { children: ReactNode }) {
  const meta = useMeta()

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="site-header">
        <div className="site-header__inner">
          <Link to="/" className="wordmark">
            <span className="wordmark__badge">ATUS</span>
            Explorer
          </Link>
          <nav className="site-nav" aria-label="Main">
            <NavLink to="/explore">Explore</NavLink>
            <NavLink to="/about">About &amp; methodology</NavLink>
          </nav>
        </div>
      </header>
      <main id="main">{children}</main>
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
