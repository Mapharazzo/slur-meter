import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../../api'
import StatusBadge from '../shared/StatusBadge'
import JobSubmit from './JobSubmit'

const FILTERS = [
  { key: null, label: 'All' },
  { key: 'queued', label: 'Queued' },
  { key: 'done', label: 'Done' },
  { key: 'failed', label: 'Failed' },
]

export default function JobList() {
  const [jobs, setJobs] = useState([])
  const [filter, setFilter] = useState(null)

  useEffect(() => {
    const poll = async () => {
      try {
        const params = filter ? { status: filter } : {}
        setJobs(await api.listJobs(params))
      } catch (e) { /* ignore */ }
    }
    poll()
    const id = setInterval(poll, 3000)
    return () => clearInterval(id)
  }, [filter])

  return (
    <div className="max-w-5xl mx-auto space-y-6">
      <JobSubmit />

      {/* Filters */}
      <div className="flex gap-2">
        {FILTERS.map(f => (
          <button
            key={f.key || 'all'}
            onClick={() => setFilter(f.key)}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-all ${
              filter === f.key ? 'bg-white/10 text-white' : 'text-gray-500 hover:text-white'
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* Job list */}
      <div className="space-y-2">
        {jobs.length === 0 && (
          <p className="text-center text-gray-600 py-12">No jobs match this filter</p>
        )}
        {jobs.map(job => (
          <Link
            key={job.imdb_id}
            to={`/jobs/${job.imdb_id}`}
            className="block glass rounded-xl p-4 hover:bg-white/[0.04] transition-all"
          >
            <div className="flex items-center gap-4">
              <StatusBadge status={job.status} />
              <div className="flex-1 min-w-0">
                <div className="font-semibold truncate">{job.label}</div>
                <div className="text-xs text-gray-600 mt-0.5">
                  {job.imdb_id} &middot; {new Date(job.created_at).toLocaleString()}
                </div>
              </div>
              {job.progress > 0 && job.progress < 100 && (
                <div className="w-24">
                  <div className="w-full bg-white/5 rounded-full h-1.5">
                    <div
                      className="h-1.5 rounded-full bg-gradient-to-r from-red-500 to-purple-600 transition-all duration-500"
                      style={{ width: `${job.progress}%` }}
                    />
                  </div>
                </div>
              )}
              {job.status === 'done' && job.analysis_json?.summary && (
                <div className="text-xs text-gray-500">
                  {job.analysis_json.summary.rating}
                </div>
              )}
              {job.status === 'failed' && (
                <span className="text-red-400 text-xs truncate max-w-[180px]">{job.error}</span>
              )}
            </div>
          </Link>
        ))}
      </div>
    </div>
  )
}
