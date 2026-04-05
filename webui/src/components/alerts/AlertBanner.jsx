import { Link } from 'react-router-dom'

export default function AlertBanner({ count = 0 }) {
  if (count === 0) return null

  return (
    <Link
      to="/alerts"
      className="block bg-red-500/10 border-b border-red-500/20 px-6 py-2.5 hover:bg-red-500/15 transition-colors"
    >
      <div className="max-w-5xl mx-auto flex items-center gap-3 text-sm">
        <span className="text-red-400 font-bold">
          {count} failure{count !== 1 ? 's' : ''}
        </span>
        <span className="text-red-400/60">Click to view alerts</span>
      </div>
    </Link>
  )
}
