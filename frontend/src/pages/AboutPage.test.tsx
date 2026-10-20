/**
 * The About page states what the service logs and for how long (brief §12,
 * audit F61), matching the logging design of ADR-011 and ADR-014.
 */
import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { renderWithProviders } from '../test/render'
import { AboutPage } from './AboutPage'

function privacySection(): HTMLElement {
  const heading = screen.getByRole('heading', { name: 'Privacy and logs' })
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
    expect(text).toMatch(/no accounts, sets no cookies, stores nothing in your browser/)
    expect(text).toMatch(/no third-party analytics, trackers/)
  })

  it('lists what is logged per request, with IP addresses shortened and no bodies', () => {
    renderWithProviders(<AboutPage />)
    const section = privacySection()
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
    expect(text).toMatch(/keyed hash of the shortened address.*changes every day/)
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
