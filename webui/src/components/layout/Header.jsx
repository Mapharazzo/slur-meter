import { Link, useLocation } from 'react-router-dom'

import { useApp } from '../../context/AppContext'

const TITLES = {
  '/': 'Operations queue',
  '/jobs': 'Operations queue',
  '/leaderboard': 'Leaderboard',
  '/costs': 'Costs',
  '/revenue': 'Revenue',
  '/alerts': 'Alerts',
}

function routeTitle(pathname) {
  if (pathname.startsWith('/jobs/')) return 'Run workspace'
  return TITLES[pathname] || 'Operations control'
}

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
      {operatorToken && (
        <button type="button" className="button button--quiet" onClick={clearOperatorToken}>
          Lock operations
        </button>
      )}
    </header>
  )
}
