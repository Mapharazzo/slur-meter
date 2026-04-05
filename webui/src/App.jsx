import { useState, useEffect } from 'react'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { api } from './api'

import Sidebar from './components/layout/Sidebar'
import Header from './components/layout/Header'
import AlertBanner from './components/alerts/AlertBanner'

import Overview from './components/dashboard/Overview'
import JobList from './components/jobs/JobList'
import JobDetail from './components/jobs/JobDetail'
import Leaderboard from './components/leaderboard/Leaderboard'
import CostDashboard from './components/costs/CostDashboard'
import RevenueDashboard from './components/revenue/RevenueDashboard'
import AlertList from './components/alerts/AlertList'

export default function App() {
  const [alertCount, setAlertCount] = useState(0)

  useEffect(() => {
    const poll = async () => {
      try {
        const alerts = await api.getAlerts(100)
        setAlertCount(alerts.length)
      } catch (e) { /* ignore */ }
    }
    poll()
    const id = setInterval(poll, 10000)
    return () => clearInterval(id)
  }, [])

  return (
    <BrowserRouter>
      <div className="flex min-h-screen">
        <Sidebar alertCount={alertCount} />
        <div className="flex-1 flex flex-col min-w-0">
          <AlertBanner count={alertCount} />
          <Header />
          <main className="flex-1 p-6 overflow-auto">
            <Routes>
              <Route path="/" element={<Overview />} />
              <Route path="/jobs" element={<JobList />} />
              <Route path="/jobs/:imdbId" element={<JobDetail />} />
              <Route path="/leaderboard" element={<Leaderboard />} />
              <Route path="/costs" element={<CostDashboard />} />
              <Route path="/revenue" element={<RevenueDashboard />} />
              <Route path="/alerts" element={<AlertList />} />
            </Routes>
          </main>
        </div>
      </div>
    </BrowserRouter>
  )
}
