import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../../api'
import StatsGrid from './StatsGrid'
import StatusBadge from '../shared/StatusBadge'
import JobSubmit from '../jobs/JobSubmit'

export default function Overview() {
  const [jobs, setJobs] = useState([])
  const [alerts, setAlerts] = useState([])

  useEffect(() => {
    const poll = async () => {
      try {
        const [j, a] = await Promise.all([
          api.listJobs({ limit: 20 }),
          api.getAlerts(5),
        ])
        setJobs(j)
        setAlerts(a)
      } catch (e) { /* ignore */ }
    }
    poll()
    const id = setInterval(poll, 3000)
    return () => clearInterval(id)
  }, [])

  const hasActive = jobs.some(j =>
    ['queued', 'fetching', 'analysing', 'rendering', 'encoding'].includes(j.status)
  )

  return (
    <div className="max-w-5xl mx-auto space-y-8">
      <StatsGrid jobs={jobs} />

      {/* Alert banner */}
      {alerts.length > 0 && (
        <Link to="/alerts"
          className="block glass rounded-xl p-4 border border-red-500/20 hover:border-red-500/40 transition-colors">
          <div className="flex items-center gap-3">
            <span className="text-red-400 font-bold text-sm">
              {alerts.length} alert{alerts.length !== 1 ? 's' : ''}
            </span>
            <span className="text-gray-500 text-sm truncate">
              {alerts[0]?.message}
            </span>
          </div>
        </Link>
      )}

      {/* Submit */}
      <JobSubmit />

      {/* Recent jobs */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="font-bold text-gray-400 text-sm uppercase tracking-wider">Recent Jobs</h3>
          <Link to="/jobs" className="text-xs text-gray-600 hover:text-white transition-colors">View all</Link>
        </div>
        {jobs.length === 0 && (
          <p className="text-center text-gray-600 py-8">No jobs yet — submit a movie above</p>
        )}
        {jobs.slice(0, 10).map(job => (
          <Link
            key={job.imdb_id}
            to={`/jobs/${job.imdb_id}`}
            className="block glass rounded-xl p-4 hover:bg-white/[0.04] transition-all"
          >
            <div className="flex items-center gap-4">
              <StatusBadge status={job.status} />
              <div className="flex-1 min-w-0">
                <div className="font-semibold truncate">{job.label}</div>
                <div className="text-xs text-gray-600 mt-0.5">{job.imdb_id}</div>
              </div>
              {job.status === 'done' && job.analysis_json?.summary && (
                <div className="text-right text-xs">
                  <span className="text-red-400 font-mono">{job.analysis_json.summary.total_hard || 0} hard</span>
                  <span className="text-gray-700 mx-1">|</span>
                  <span className="text-purple-400 font-mono">{job.analysis_json.summary.total_f_bombs || 0} f</span>
                </div>
              )}
              {hasActive && ['queued', 'fetching', 'analysing', 'rendering', 'encoding'].includes(job.status) && (
                <div className="text-xs text-gray-500 animate-pulse-slow max-w-[180px] truncate">
                  {job.message}
                </div>
              )}
              {job.status === 'failed' && (
                <span className="text-red-400 text-xs truncate max-w-[200px]">{job.error}</span>
              )}
            </div>
          </Link>
        ))}
      </div>
    </div>
  )
}
