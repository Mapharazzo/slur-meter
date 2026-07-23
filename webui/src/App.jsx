import { Component, useState } from 'react'
import { BrowserRouter, Link, Route, Routes, useLocation } from 'react-router-dom'

import AlertList from './components/alerts/AlertList'
import CostDashboard from './components/costs/CostDashboard'
import OperationsOverview from './components/dashboard/OperationsOverview'
import JobDetail from './components/jobs/JobDetail'
import Header from './components/layout/Header'
import Sidebar from './components/layout/Sidebar'
import SystemStatusBar from './components/layout/SystemStatusBar'
import Leaderboard from './components/leaderboard/Leaderboard'
import RevenueDashboard from './components/revenue/RevenueDashboard'
import ToastRegion from './components/shared/ToastRegion'
import { AppProvider, useApp } from './context/AppContext'

export class RouteErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { failed: false }
  }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch() {
    // React reports the component stack in development. Operator-safe UI stays generic.
  }

  render() {
    if (this.state.failed) {
      return (
        <section role="alert" className="route-state">
          <p className="eyebrow">Route unavailable</p>
          <h1>This screen could not be displayed.</h1>
          <p>The failure was contained. No diagnostic or credential data is shown here.</p>
          <button type="button" className="button button--primary" onClick={() => this.setState({ failed: false })}>
            Try this screen again
          </button>
        </section>
      )
    }
    return this.props.children
  }
}

function UnlockScreen() {
  const [value, setValue] = useState('')
  const [error, setError] = useState('')
  const { setOperatorToken } = useApp()

  const unlock = (event) => {
    event.preventDefault()
    const token = value.trim()
    if (!token) {
      setError('Enter the operator token for this session.')
      return
    }
    setOperatorToken(token)
    setValue('')
  }

  return (
    <section className="unlock-panel" aria-labelledby="unlock-heading">
      <p className="eyebrow">Protected operations</p>
      <h1 id="unlock-heading">Unlock operations</h1>
      <p>The token stays in session storage and is sent only in authenticated request headers.</p>
      <form onSubmit={unlock}>
        <label className="field-label">
          <span>Operator token</span>
          <input
            type="password"
            value={value}
            onChange={(event) => { setValue(event.target.value); setError('') }}
            autoComplete="current-password"
            aria-invalid={Boolean(error)}
          />
        </label>
        {error && <p role="alert" className="inline-error">{error}</p>}
        <button type="submit" className="button button--primary">Unlock</button>
      </form>
    </section>
  )
}

function Protected({ children }) {
  const { operatorToken } = useApp()
  return operatorToken ? children : <UnlockScreen />
}

function NotFound() {
  return (
    <section className="route-state">
      <p className="eyebrow">404</p>
      <h1>Page not found</h1>
      <p>This route does not exist in the operations control panel.</p>
      <Link to="/" className="button button--primary">Return to operations</Link>
    </section>
  )
}

function ShellRoutes({ poll }) {
  const location = useLocation()
  return (
    <RouteErrorBoundary key={location.pathname}>
      <Routes>
        <Route path="/" element={<Protected><OperationsOverview poll={poll} /></Protected>} />
        <Route path="/jobs" element={<Protected><OperationsOverview poll={poll} /></Protected>} />
        <Route path="/jobs/:imdbId" element={<Protected><JobDetail /></Protected>} />
        <Route path="/leaderboard" element={<Protected><Leaderboard /></Protected>} />
        <Route path="/costs" element={<Protected><CostDashboard /></Protected>} />
        <Route path="/revenue" element={<Protected><RevenueDashboard /></Protected>} />
        <Route path="/alerts" element={<Protected><AlertList /></Protected>} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </RouteErrorBoundary>
  )
}

function ApplicationShell({ poll }) {
  const { toasts, dismissToast } = useApp()
  return (
    <>
      <a href="#main-content" className="skip-link">Skip to main content</a>
      <div className="app-shell">
        <Sidebar />
        <div className="app-frame">
          <SystemStatusBar />
          <Header />
          <main id="main-content" tabIndex="-1">
            <ShellRoutes poll={poll} />
          </main>
        </div>
      </div>
      <ToastRegion toasts={toasts} onDismiss={dismissToast} />
    </>
  )
}

export default function App({ poll = true }) {
  return (
    <BrowserRouter>
      <AppProvider>
        <ApplicationShell poll={poll} />
      </AppProvider>
    </BrowserRouter>
  )
}
