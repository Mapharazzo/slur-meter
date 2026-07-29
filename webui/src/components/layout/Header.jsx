import { Link, useLocation } from 'react-router-dom'

import { useApp } from '../../context/AppContext'
import AlertBanner from '../alerts/AlertBanner'
import SystemStatusBar from './SystemStatusBar'

const TITLES = {
  '/': 'Operations queue',
  '/jobs': 'Operations queue',
  '/completed': 'Completed videos',
  '/leaderboard': 'Completed videos',
  '/costs': 'Costs',
  '/revenue': 'Revenue',
  '/alerts': 'Alerts',
}

function routeTitle(pathname) {
  if (pathname.startsWith('/jobs/')) return 'Run workspace'
  return TITLES[pathname] || 'Operations control'
}

// System health and the attention total each used to occupy a full-width bar of
// their own above the page. They carry one reading apiece, so they ride here as
// chips instead and give roughly 90px of vertical room back to the content.
export default function Header() {
  const location = useLocation()
  const { operatorToken, clearOperatorToken } = useApp()
  const title = routeTitle(location.pathname)

  return (
    <header className="topbar">
      <div className="topbar__route">
        <Link to="/">Control panel</Link>
        <span aria-hidden="true">/</span>
        <span aria-current="page">{title}</span>
      </div>
      <div className="topbar__tools">
        <AlertBanner />
        <SystemStatusBar />
        {operatorToken && (
          <button type="button" className="button button--quiet button--sm" onClick={clearOperatorToken}>
            Lock operations
          </button>
        )}
      </div>
    </header>
  )
}
