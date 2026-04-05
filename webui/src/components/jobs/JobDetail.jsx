import { useState, useEffect } from 'react'
import { useParams, Link } from 'react-router-dom'
import { api } from '../../api'
import StatusBadge from '../shared/StatusBadge'
import StatCard from '../shared/StatCard'
import PipelineSteps from './PipelineSteps'
import VideoPreview from '../video/VideoPreview'
import CostBreakdown from '../costs/CostBreakdown'

export default function JobDetail() {
  const { imdbId } = useParams()
  const [job, setJob] = useState(null)
  const [costs, setCosts] = useState([])
  const [error, setError] = useState(null)

  useEffect(() => {
    const poll = async () => {
      try {
        const [j, c] = await Promise.all([
          api.getJob(imdbId),
          api.getJobCosts(imdbId).catch(() => []),
        ])
        setJob(j)
        setCosts(c)
        setError(null)
      } catch (e) {
        setError(e.message)
      }
    }
    poll()

    // Poll while job is active
    const id = setInterval(poll, 2000)
    return () => clearInterval(id)
  }, [imdbId])

  if (error) {
    return (
      <div className="max-w-3xl mx-auto py-12 text-center">
        <div className="text-red-400 text-lg font-bold">Job not found</div>
        <p className="text-gray-500 mt-2 text-sm">{error}</p>
        <Link to="/jobs" className="text-gray-600 hover:text-white text-sm mt-4 inline-block">Back to jobs</Link>
      </div>
    )
  }

  if (!job) {
    return <div className="text-center py-12 text-gray-600 animate-pulse-slow">Loading...</div>
  }

  const isActive = ['queued', 'fetching', 'analysing', 'rendering', 'encoding'].includes(job.status)
  const isDone = job.status === 'done'
  const summary = job.analysis_json?.summary || {}

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <Link to="/jobs" className="text-gray-600 hover:text-white text-sm transition-colors">
        &larr; All Jobs
      </Link>

      {/* Header */}
      <div className="glass rounded-2xl p-6 space-y-4">
        <div className="flex items-center gap-3">
          <StatusBadge status={job.status} />
          <h2 className="text-xl font-bold flex-1">{job.label}</h2>
          <span className="text-xs text-gray-600 font-mono">{job.imdb_id}</span>
        </div>

        {/* Progress bar */}
        {isActive && (
          <>
            <div className="w-full bg-white/5 rounded-full h-2">
              <div
                className="h-2 rounded-full bg-gradient-to-r from-red-500 to-purple-600 transition-all duration-1000"
                style={{ width: `${job.progress || 0}%` }}
              />
            </div>
            <p className="text-sm text-gray-400 animate-pulse-slow">{job.message}</p>
          </>
        )}

        {job.status === 'failed' && (
          <div className="bg-red-500/10 border border-red-500/20 rounded-lg p-4">
            <div className="text-red-400 font-bold text-sm">Pipeline Failed</div>
            <p className="text-red-400/70 text-sm mt-1">{job.error}</p>
          </div>
        )}
      </div>

      {/* Pipeline steps */}
      <PipelineSteps steps={job.steps || []} />

      {/* Results */}
      {isDone && (
        <div className="glass rounded-2xl p-6 space-y-6">
          <h3 className="text-sm font-bold text-gray-400 uppercase tracking-wider">Results</h3>

          <div className="text-center">
            <div className="text-3xl font-bold bg-gradient-to-r from-red-500 to-purple-600 bg-clip-text text-transparent">
              {summary.rating || 'Unrated'}
            </div>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <StatCard label="Hard Slurs" value={summary.total_hard || 0} color="text-red-400" />
            <StatCard label="Soft Slurs" value={summary.total_soft || 0} color="text-yellow-400" />
            <StatCard label="F-Bombs" value={summary.total_f_bombs || 0} color="text-purple-400" />
            <StatCard label="Peak Score" value={summary.peak_score || 0} color="text-green-400"
              sub={summary.peak_minute ? `min ${summary.peak_minute}` : null} />
          </div>

          <div className="flex justify-between text-xs text-gray-600">
            <span>Runtime: {summary.runtime_minutes || '?'} min</span>
            <span>Words counted: {summary.total_words_counted || '?'}</span>
          </div>
        </div>
      )}

      {/* Video preview */}
      {isDone && (
        <VideoPreview imdbId={imdbId} segmentTiming={job.segment_timing} />
      )}

      {/* Costs */}
      {costs.length > 0 && (
        <CostBreakdown costs={costs} />
      )}
    </div>
  )
}
