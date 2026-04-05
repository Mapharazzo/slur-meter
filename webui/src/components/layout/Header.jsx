import { useLocation, Link } from 'react-router-dom'

const TITLES = {
  '/': 'Overview',
  '/jobs': 'Jobs',
  '/leaderboard': 'Leaderboard',
  '/costs': 'Costs',
  '/revenue': 'Revenue',
  '/alerts': 'Alerts',
}

export default function Header() {
  const location = useLocation()

  // Find matching title (handles nested routes like /jobs/tt0110912)
  const title = Object.entries(TITLES).reduce((best, [path, t]) => {
    if (location.pathname === path || (path !== '/' && location.pathname.startsWith(path))) {
      return t
    }
    return best
  }, 'Overview')

  // Build breadcrumbs from path
  const parts = location.pathname.split('/').filter(Boolean)

  return (
    <header className="h-14 border-b border-white/5 bg-[#0d0d0d]/80 backdrop-blur-md flex items-center px-6 gap-4">
      <div className="flex items-center gap-2 text-sm">
        <Link to="/" className="text-gray-500 hover:text-white transition-colors">Home</Link>
        {parts.map((part, i) => {
          const path = '/' + parts.slice(0, i + 1).join('/')
          const isLast = i === parts.length - 1
          return (
            <span key={path} className="flex items-center gap-2">
              <span className="text-gray-700">/</span>
              {isLast ? (
                <span className="text-white font-medium">{part}</span>
              ) : (
                <Link to={path} className="text-gray-500 hover:text-white transition-colors">
                  {part}
                </Link>
              )}
            </span>
          )
        })}
      </div>
    </header>
  )
}
