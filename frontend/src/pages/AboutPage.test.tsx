/**
 * The About page states what the service logs and for how long (brief §12,
 * audit F61), matching the logging design of ADR-011 and ADR-014.
 */
import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { PREWAKE_INTERVAL_MS } from '../api/prewake'
import { renderWithProviders } from '../test/render'
import { AboutPage } from './AboutPage'

function privacySection(): HTMLElement {
  const heading = screen.getByRole('heading', { name: 'Your privacy' })
  const section = document.createElement('div')
  // The section runs from its heading to the next h2.
  let node = heading.nextElementSibling
  while (node && node.tagName !== 'H2') {
    section.append(node.cloneNode(true))
    node = node.nextElementSibling
  }
  return section
}

describe('About page: privacy and logs', () => {
  it('says there are no accounts, cookies or third-party analytics', () => {
    renderWithProviders(<AboutPage />)
    const text = privacySection().textContent ?? ''
    expect(text).toMatch(
      /no accounts, sets no cookies, keeps nothing in your browser's\s+storage/,
    )
    expect(text).toMatch(/no third-party analytics, trackers/)
  })

  it('opens with a plain summary, the full account one click away (review round 6)', () => {
    renderWithProviders(<AboutPage />)
    const section = privacySection()
    const summary = Array.from(section.querySelectorAll(':scope > ul > li')).map(
      (item) => item.textContent,
    )
    expect(summary).toEqual([
      'No accounts, cookies, trackers or third-party scripts.',
      expect.stringMatching(/most common charts come from files on this site/),
      expect.stringMatching(
        /IP address shortened, and the hosts delete those logs within 7 days/,
      ),
    ])
    expect(section.querySelector('details > summary')?.textContent).toBe(
      'Everything that is logged, and for how long',
    )
  })

  it('lists what is logged per request, with IP addresses shortened and no bodies', () => {
    renderWithProviders(<AboutPage />)
    const section = privacySection().querySelector('details') as HTMLElement
    const items = within(section)
      .getAllByRole('listitem')
      .map((item) => item.textContent)
    expect(items).toEqual([
      expect.stringMatching(/random request ID.*status.*timing/),
      expect.stringMatching(/cache.*analysis key.*not the specification itself/),
      expect.stringMatching(/IP address shortened to its network.*IPv4.*48 bits.*IPv6/),
    ])
    const text = section.textContent ?? ''
    expect(text).toContain('Request bodies are never logged.')
    expect(text).toMatch(/full IP address only in memory/)
    expect(text).toMatch(/keyed hash of the shortened address.*derived anew each day/)
  })

  it('says deleted usage counts stay restorable for 30 days (review round 2)', () => {
    // The governor's purge is a DELETE in a SQLite-backed Durable Object, whose
    // point-in-time recovery reaches back 30 days (docs/security.md, C17).
    renderWithProviders(<AboutPage />)
    const text = privacySection().textContent ?? ''
    expect(text).toMatch(/deletes those counts after each day/)
    expect(text).toMatch(/restore deleted data for up to 30 days/)
  })

  it('names both triggers of the empty wake request and its rate (review round 2)', () => {
    // prewake() runs on entering Explore and from runAnalysis for any analysis
    // the static snapshot does not hold, at most once per PREWAKE_INTERVAL_MS.
    renderWithProviders(<AboutPage />)
    const text = (privacySection().textContent ?? '').replace(/\s+/g, ' ')
    expect(text).toMatch(
      /Opening the analysis builder, or an analysis that is not among the pre-computed ones, also sends one empty request/,
    )
    expect(text).toContain(`at most once every ${PREWAKE_INTERVAL_MS / 60_000} minutes`)
  })

  it('states each provider retention period', () => {
    renderWithProviders(<AboutPage />)
    const text = privacySection().textContent ?? ''
    expect(text).toMatch(/\(Render\) keeps them for 7 days/)
    expect(text).toMatch(
      /Cloudflare Workers Logs\) for 3 days \(7 days\s+from 1 December 2026\)/,
    )
    expect(text).toMatch(/under their own\s+privacy policies/)
  })
})

describe('About page: how sure the numbers are (review round 6)', () => {
  it('explains the 95% confidence interval where the results link to it', () => {
    renderWithProviders(<AboutPage />)
    const heading = screen.getByRole('heading', { name: 'How sure are the numbers?' })
    expect(heading).toHaveAttribute('id', 'how-sure')
    const text = (heading.nextElementSibling?.textContent ?? '').replace(/\s+/g, ' ')
    expect(text).toContain(
      'if the survey were repeated many times with new samples, about 95% of the intervals it produced would contain the true value for the whole population',
    )
    expect(text).toContain('The interval reflects sampling error only')
    expect(document.body.textContent).not.toMatch(/likely range/i)
  })
})
