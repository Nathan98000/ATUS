import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router'

import { App } from './App'
import { ErrorBoundary } from './components/ErrorBoundary'
// The display face, self-hosted (font-src 'self'): weight and width axes.
import '@fontsource-variable/archivo/wdth.css'
import './styles/global.css'

// Every query sets its own policy (src/api/queries.ts); these defaults only
// make sure nothing refetches behind the visitor's back: a focus refetch can
// wake a sleeping free-tier host for nothing (audit F62), and API retries are
// handled by the time-budgeted loop in src/api/resilience.ts.
const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false, retry: false } },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
)
