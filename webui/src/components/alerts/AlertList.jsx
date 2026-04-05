import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../../api'

export default function AlertList() {
  const [alerts, setAlerts] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const load = async () => {
      try {
        setAlerts(await api.getAlerts(100))
      } catch (e) { /* ignore */ }
      finally { setLoading(false) }
    }
    load()
    const id = setInterval(load, 5000)
    return () => clearInterval(id)
  }, [])

  if (loading) {
    return <div className="text-center py-12 text-gray-600 animate-pulse-slow">Loading alerts...</div>
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-bold">Alerts</h2>
        {alerts.length > 0 && (
          <span className="bg-red-500/20 text-red-400 text-xs font-bold px-2 py-1 rounded-full">
            {alerts.length} active
          </span>
        )}
      </div>

      {alerts.length === 0 ? (
        <div className="glass rounded-xl p-12 text-center space-y-2">
          <div className="text-3xl">✅</div>
          <p className="text-gray-500">No alerts — all clear</p>
        </div>
      ) : (
        <div className="space-y-3">
          {alerts.map((alert, i) => (
            <Link
              key={i}
              to={`/jobs/${alert.imdb_id}`}
              className="block glass rounded-xl p-4 border border-red-500/20 hover:border-red-500/40 transition-all"
            >
              <div className="flex items-start gap-3">
                <span className="text-red-400 text-lg mt-0.5">
                  {alert.alert_type === 'job' ? '💥' : '📤'}
                </span>
                <div className="flex-1 min-w-0">
                  <div className="font-semibold truncate">{alert.label}</div>
                  <div className="text-sm text-red-400/70 mt-0.5 truncate">{alert.message}</div>
                  <div className="text-xs text-gray-600 mt-1">
                    {alert.alert_type === 'job' ? 'Pipeline failure' : 'Release failure'} &middot;{' '}
                    {alert.timestamp ? new Date(alert.timestamp).toLocaleString() : '—'}
                  </div>
                </div>
                <span className="text-xs text-gray-600 font-mono shrink-0">{alert.imdb_id}</span>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}
