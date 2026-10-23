import { Link } from 'react-router'

/** An address with nothing at it, and the two ways on (review round 6). */
export function NotFoundPage() {
  return (
    <div className="card notice-page">
      <h1 tabIndex={-1}>Page not found</h1>
      <p>There is nothing at this address.</p>
      <p className="notice-page__actions">
        <Link className="btn btn--primary" to="/explore">
          Make a chart
        </Link>
        <Link className="btn" to="/">
          Home
        </Link>
      </p>
    </div>
  )
}
