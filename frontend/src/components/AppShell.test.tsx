/**
 * Links to a part of a page land on that part (review round 6): a result's
 * "What does 95% mean?" opens the About page at "How sure are the numbers?".
 */
import { QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Link, MemoryRouter, Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'

import { AboutPage } from '../pages/AboutPage'
import { testQueryClient } from '../test/render'
import { AppShell } from './AppShell'

function renderAt(route: string) {
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <MemoryRouter initialEntries={[route]}>
        <AppShell>
          <Routes>
            <Route path="/about" element={<AboutPage />} />
            <Route
              path="/"
              element={
                <>
                  <h1 tabIndex={-1}>Home</h1>
                  <Link to="/about#how-sure">What does 95% mean?</Link>
                </>
              }
            />
          </Routes>
        </AppShell>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('links to a part of a page (review round 6)', () => {
  it('a first visit to /about#how-sure lands on that section', () => {
    renderAt('/about#how-sure')
    expect(screen.getByRole('heading', { name: 'How sure are the numbers?' })).toHaveFocus()
  })

  it('a link from another page lands on the section, not the page heading', async () => {
    const user = userEvent.setup()
    renderAt('/')
    await user.click(screen.getByRole('link', { name: 'What does 95% mean?' }))
    expect(screen.getByRole('heading', { name: 'How sure are the numbers?' })).toHaveFocus()
  })

  it('the About page’s footer does not link to the About page', () => {
    renderAt('/about')
    expect(screen.queryByRole('link', { name: 'About page' })).not.toBeInTheDocument()
  })
})
