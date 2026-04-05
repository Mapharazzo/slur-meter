import { Link, useLocation } from 'react-router-dom'

const NAV = [
  { path: '/',            label: 'Overview',     icon: '📊' },
  { path: '/jobs',        label: 'Jobs',         icon: '🎬' },
  { path: '/leaderboard', label: 'Leaderboard',  icon: '🏆' },
  { path: '/costs',       label: 'Costs',        icon: '💰' },
  { path: '/revenue',     label: 'Revenue',      icon: '📈' },
  { path: '/alerts',      label: 'Alerts',       icon: '🔔' },
]

export default function Sidebar({ alertCount = 0 }) {
  const location = useLocation()

  return (
    <aside className="w-56 min-h-screen border-r border-white/5 bg-[#0d0d0d] flex flex-col">
      <div className="p-5 border-b border-white/5">
        <Link to="/" className="flex items-center gap-2">
          <span className="text-2xl">📉</span>
          <div>
            <h1 className="text-sm font-bold bg-gradient-to-r from-red-500 to-purple-500 bg-clip-text text-transparent">
              Daily Slur Meter
            </h1>
            <span className="text-[10px] text-gray-600 uppercase tracking-widest">Admin</span>
          </div>
        </Link>
      </div>

      <nav className="flex-1 p-3 space-y-1">
        {NAV.map(item => {
          const active = item.path === '/'
            ? location.pathname === '/'
            : location.pathname.startsWith(item.path)
          return (
            <Link
              key={item.path}
              to={item.path}
              className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-all ${
                active
                  ? 'bg-white/10 text-white font-medium'
                  : 'text-gray-500 hover:text-white hover:bg-white/[0.04]'
              }`}
            >
              <span className="text-base">{item.icon}</span>
              <span>{item.label}</span>
              {item.path === '/alerts' && alertCount > 0 && (
                <span className="ml-auto bg-red-500/20 text-red-400 text-xs font-bold px-1.5 py-0.5 rounded-full">
                  {alertCount}
                </span>
              )}
            </Link>
          )
        })}
      </nav>

      <div className="p-4 border-t border-white/5 text-[10px] text-gray-700">
        v2.0 — Admin Dashboard
      </div>
    </aside>
  )
}
