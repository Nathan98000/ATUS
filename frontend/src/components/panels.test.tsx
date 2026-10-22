import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import { ApiError, NetworkError } from '../api/client'
import { contextNotes } from '../domain/notes'
import { estimatePandemicFixture } from '../test/fixtures'
import { ErrorPanel } from './ErrorPanel'
import { WarningBanner } from './WarningBanner'

describe('WarningBanner', () => {
  it('renders every warning from the API, in plain language', () => {
    render(<WarningBanner warnings={estimatePandemicFixture.warnings} />)
    const note = screen.getByRole('note')
    expect(note).toHaveTextContent(/May 10 – December 31/)
    expect(note).toHaveTextContent(/not full-year figures/)
    expect(note).not.toHaveTextContent(/TU20FWGT/)
  })

  it('shows caveats as a warning and leaves context to the chart’s small print', () => {
    const warnings = [
      'Activity codes use the BLS-harmonized 2003-25 multi-year lexicon (cross-year comparable by construction).',
      'Pooled estimate: represents the average day across 3 survey years (1,095 days combined).',
    ]
    const { container } = render(<WarningBanner warnings={warnings} />)
    expect(container.querySelector('.warning-banner')).toHaveTextContent(
      'This is an average day across 3 survey years combined',
    )
    expect(container).not.toHaveTextContent(/coded the same/)
    expect(contextNotes(warnings)).toEqual([
      'Activities are coded the same way in every survey year, so years can be compared directly.',
    ])
  })

  it('renders nothing when every note is context', () => {
    render(
      <WarningBanner
        warnings={[
          'Activity codes use the BLS-harmonized 2003-25 multi-year lexicon (cross-year comparable by construction).',
        ]}
      />,
    )
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
  })

  it('shows a warning it does not recognize exactly as the API wrote it, as a caveat', () => {
    const { container } = render(<WarningBanner warnings={['A brand-new caveat (XYZW).']} />)
    expect(container.querySelector('.warning-banner')).toHaveTextContent(
      'A brand-new caveat (XYZW).',
    )
  })

  it('renders nothing when there are no warnings', () => {
    render(<WarningBanner warnings={[]} />)
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
  })
})

describe('ErrorPanel', () => {
  it('shows the API message for unsupported analyses with an adjust action', () => {
    const error = new ApiError(
      422,
      'unsupported_analysis',
      'TUFNWGTP (the multi-year weight) is undefined for 2020…',
      null,
      'req-1',
    )
    render(
      <ErrorPanel error={error} adjustHref="/explore?op=estimate&spec=x" onRetry={() => {}} />,
    )
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('not statistically supported')
    expect(alert).toHaveTextContent('TUFNWGTP (the multi-year weight) is undefined')
    expect(screen.getByRole('link', { name: 'Adjust analysis' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
    expect(alert).toHaveTextContent('HTTP 422 · unsupported_analysis · request req-1')
  })

  it('offers retry for network failures without blaming the request', () => {
    const onRetry = vi.fn()
    render(
      <ErrorPanel error={new NetworkError('boom')} onRetry={onRetry} adjustHref="/explore" />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('could not be reached')
    screen.getByRole('button', { name: 'Try again' }).click()
    expect(onRetry).toHaveBeenCalled()
    expect(screen.queryByRole('link', { name: 'Adjust analysis' })).not.toBeInTheDocument()
  })

  it('treats 503 as temporary unavailability with retry', () => {
    const error = new ApiError(503, 'database_unavailable', 'db down', null, null)
    render(<ErrorPanel error={error} onRetry={() => {}} />)
    expect(screen.getByRole('alert')).toHaveTextContent('temporarily unavailable')
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })

  it('a Retry-After holds "Try again" back, keeping the countdown out of the live region', () => {
    const error = new ApiError(503, 'overloaded', 'busy', null, null, 12)
    render(<ErrorPanel error={error} onRetry={() => {}} />)
    const button = screen.getByRole('button', { name: 'Try again' })
    expect(button).toBeDisabled()
    expect(button.querySelector('[aria-hidden="true"]')).toHaveTextContent('(12 s)')
  })

  it('a paused quota renders the full-page state instead of a panel', () => {
    const error = new ApiError(503, 'database_paused', 'quota', null, 'req-2')
    render(
      <MemoryRouter initialEntries={['/explore']}>
        <ErrorPanel error={error} onRetry={() => {}} />
      </MemoryRouter>,
    )
    expect(
      screen.getByRole('heading', { level: 1, name: 'The analysis database is paused' }),
    ).toBeInTheDocument()
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(screen.getByRole('link', { name: 'Go to the home page' })).toHaveAttribute(
      'href',
      '/',
    )
  })

  it('never exposes stack traces for unknown errors', () => {
    render(<ErrorPanel error={new Error('secret internal detail')} />)
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('Something went wrong')
    // The message only appears inside the collapsed technical details.
    expect(screen.getByText('Technical details')).toBeInTheDocument()
  })
})

describe('validation details (review finding: field errors surfaced)', () => {
  it('lists field-level problems from 422 validation details', () => {
    // The API's envelope (errors.py validation_details): {location, message, type}.
    const error = new ApiError(
      422,
      'validation_error',
      'Request validation failed.',
      {
        errors: [
          {
            location: ['body', 'population', 'age_min'],
            message: 'Input should be less than or equal to 130',
            type: 'less_than_equal',
          },
          {
            location: ['body', 'years', 0],
            message: 'Input should be greater than or equal to 2003',
            type: 'greater_than_equal',
          },
        ],
      },
      null,
    )
    render(<ErrorPanel error={error} adjustHref="/explore" />)
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent(
      'population.age_min: Input should be less than or equal to 130',
    )
    expect(alert).toHaveTextContent('years.0: Input should be greater than or equal to 2003')
  })
})
