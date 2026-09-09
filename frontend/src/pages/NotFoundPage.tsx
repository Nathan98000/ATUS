import { Link } from 'react-router'

export function NotFoundPage() {
  return (
    <div style={{ maxWidth: '32rem', margin: '3rem auto', textAlign: 'center' }}>
      <h1>Page not found</h1>
      <p>There is nothing at this address.</p>
      <p>
        <Link className="btn" to="/">
          Back to ATUS Explorer
        </Link>
      </p>
    </div>
  )
}
