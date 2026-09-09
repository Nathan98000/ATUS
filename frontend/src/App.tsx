import { Route, Routes } from 'react-router'

import { AppShell } from './components/AppShell'
import { AboutPage } from './pages/AboutPage'
import { AnalysisPage } from './pages/AnalysisPage'
import { ExplorePage } from './pages/ExplorePage'
import { HomePage } from './pages/HomePage'
import { NotFoundPage } from './pages/NotFoundPage'

export function App() {
  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/explore" element={<ExplorePage />} />
        <Route path="/analysis/:operation" element={<AnalysisPage />} />
        <Route path="/about" element={<AboutPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </AppShell>
  )
}
