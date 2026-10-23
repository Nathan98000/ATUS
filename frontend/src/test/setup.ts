import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterAll, afterEach, beforeAll } from 'vitest'

import { resetPrewake } from '../api/prewake'
import { resetServiceStatus } from '../api/serviceStatus'
import { resetSnapshot } from '../api/snapshot'
import { server } from './server'

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  cleanup() // vitest runs without globals, so RTL cannot auto-register this
  server.resetHandlers()
  // Module-level client state must not leak between tests.
  resetServiceStatus()
  resetSnapshot()
  resetPrewake()
})
afterAll(() => server.close())
