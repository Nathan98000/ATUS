import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ApiError, NetworkError } from '../api/client'
import { estimatePandemicFixture } from '../test/fixtures'
import { ErrorPanel } from './ErrorPanel'
import { WarningBanner } from './WarningBanner'

describe('WarningBanner', () => {
  it('renders every warning from the API', () => {
    render(<WarningBanner warnings={estimatePandemicFixture.warnings} />)
    expect(screen.getByRole('note')).toHaveTextContent(/May 10 - Dec 31/)
    expect(screen.getByRole('note')).toHaveTextContent(
      /annual estimates for 2020 are not possible/,
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
    const error = new ApiError(
      422,
      'validation_error',
      'Request validation failed.',
      {
        errors: [
          {
            loc: ['body', 'population', 'age_min'],
            msg: 'Input should be less than or equal to 130',
          },
          { loc: ['body', 'years', 0], msg: 'Input should be greater than or equal to 2003' },
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
