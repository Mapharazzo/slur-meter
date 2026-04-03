import React from 'react'

const API = '/api'

export default function Dashboard({ jobs, onJobClick, onSubmit }) {
  const inProgress = jobs.filter(j => ['queued', 'fetching', 'analysing', 'rendering'].includes(j.status)).length
  const done = jobs.filter(j => j.status === 'done').length
  const failed = jobs.filter(j => j.status === 'failed').length

  return (
    <div className="space-y-8">
      {/* Stats Row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard label="Total" value={jobs.length} color="accent" icon="📊" />
        <StatCard label="In Progress" value={inProgress} color="line" icon="⏳" />
        <StatCard label="Completed" value={done} color="accent" icon="✅" />
        <StatCard label="Failed" value={failed} color="hard" icon="💥" />
      </div>

      {/* Submit CTA */}
      <div className="glass rounded-2xl p-8 text-center">
        <h2 className="text-2xl font-bold mb-2">Rate a new movie</h2>
        <p className="text-gray-400 mb-6">Enter an IMDB ID or movie title to analyse</p>
        <button
          onClick={onSubmit}
          className="px-8 py-3 bg-gradient-to-r from-[#ff1744] to-[#d500f9] rounded-xl font-bold text-lg hover:shadow-[0_0_30px_rgba(255,23,68,0.3)] transition-all"
        >
          🎬 Submit a Movie
        </button>
      </div>

      {/* Jobs Feed */}
      <div className="space-y-3">
        <h3 className="text-lg font-bold text-gray-300">
          Recent Jobs
        </h3>
        {jobs.length === 0 ? (
          <div className="glass rounded-xl p-8 text-center text-gray-500">
            No jobs yet — submit your first movie!
          </div>
        ) : (
          jobs.map(job => (
            <button
              key={job.id}
              onClick={() => onJobClick(job.id)}
              className="w-full glass rounded-xl p-4 flex items-center gap-4 hover:bg-white/5 transition-all text-left"
            >
              {/* Status Indicator */}
              <StatusDot status={job.status} />

              {/* Info */}
              <div className="flex-1">
                <div className="font-bold text-white">{job.label || 'Unknown'}</div>
                <div className="text-xs text-gray-500 mt-1">
                  {job.id} • {new Date(job.created_at).toLocaleTimeString()}
                </div>
              </div>

              {/* Result Preview */}
              {job.status === 'done' && job.result && (
                <div className="text-right hidden sm:block">
                  <div className="text-accent font-mono text-sm">
                    🔴 {job.result.summary?.total_hard ?? 0} 💣 {job.result.summary?.total_f_bombs ?? 0}
                  </div>
                  <div className="text-xs text-gray-500 mt-1">
                    {job.result.summary?.rating ?? 'N/A'}
                  </div>
                </div>
              )}

              {/* In-progress spinner */}
              {['queued', 'fetching', 'analysing', 'rendering'].includes(job.status) && (
                <div className="text-xs text-gray-400 animate-pulse-slow">
                  {job.message}
                </div>
              )}

              {/* Failed */}
              {job.status === 'failed' && (
                <span className="text-hard text-sm">💥 Failed</span>
              )}
            </button>
          ))
        )}
      </div>
    </div>
  )
}

function StatCard({ label, value, color, icon }) {
  const colorMap = {
    accent: 'text-accent',
    hard: 'text-hard',
    soft: 'text-soft',
    line: 'text-line',
  }
  return (
    <div className="glass rounded-xl p-4 text-center">
      <div className="text-2xl mb-1">{icon}</div>
      <div className={`text-2xl font-bold ${colorMap[color] || 'text-white'}`}>
        {value}
      </div>
      <div className="text-xs text-gray-500 mt-1">{label}</div>
    </div>
  )
}

function StatusDot({ status }) {
  const colors = {
    queued: 'bg-gray-500',
    fetching: 'bg-yellow-500',
    analysing: 'bg-blue-500',
    rendering: 'bg-purple-500',
    done: 'bg-green-500',
    failed: 'bg-red-500',
  }
  return (
    <div className={`w-3 h-3 rounded-full ${colors[status] || 'bg-gray-500'}`} />
  )
}