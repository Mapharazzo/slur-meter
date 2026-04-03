import React, { useState, useEffect } from 'react'

const API = '/api'

export default function JobStatus({ jobId, onBack }) {
  const [job, setJob] = useState(null)

  useEffect(() => {
    const fetchJob = async () => {
      try {
        const res = await fetch(`${API}/jobs/${jobId}`)
        const data = await res.json()
        setJob(data)
      } catch (e) {
        //
      }
    }
    fetchJob()
    const interval = setInterval(fetchJob, 500)
    return () => clearInterval(interval)
  }, [jobId])

  if (!job) {
    return (
      <div className="text-center py-20">
        <div className="text-4xl mb-4 animate-pulse">⏳</div>
        <p className="text-gray-400">Loading job…</p>
      </div>
    )
  }

  const isActive = ['queued', 'fetching', 'analysing', 'rendering'].includes(job.status)
  const isDone = job.status === 'done'
  const isFailed = job.status === 'failed'

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      {/* Back */}
      <button
        onClick={onBack}
        className="text-gray-500 hover:text-white transition-colors"
      >
        ← Back to Dashboard
      </button>

      {/* Pipeline Progress */}
      <div className="glass rounded-2xl p-6 space-y-6">
        <div className="flex items-center justify-between">
          <h2 className="text-xl font-bold">{job.label || 'Unknown Movie'}</h2>
          <StatusBadge status={job.status} />
        </div>

        {/* Progress Bar */}
        <div className="w-full bg-white/5 rounded-full h-2">
          <div
            className="h-2 rounded-full bg-gradient-to-r from-[#ff1744] to-[#d500f9] transition-all duration-700"
            style={{ width: `${job.progress || 0}%` }}
          />
        </div>

        <p className="text-sm text-gray-400">{job.message || 'Waiting…'}</p>

        {/* Pipeline Steps */}
        <div className="grid grid-cols-5 gap-2 text-center text-xs">
          {['queued', 'fetching', 'analysing', 'rendering', 'done'].map((step, i) => {
            const stepIdx = ['queued', 'fetching', 'analysing', 'rendering', 'done'].indexOf(job.status)
            const currentIdx = i
            const doneStep = stepIdx > currentIdx || (isDone && true)
            const currentStep = currentIdx === stepIdx && isActive
            const icons = {
              queued: '📋',
              fetching: '📥',
              analysing: '🔍',
              rendering: '🎬',
              done: '✅',
            }
            return (
              <div
                key={step}
                className={`py-2 rounded-lg transition-all ${
                  doneStep ? 'bg-accent/10 text-accent' :
                  currentStep ? 'bg-line/10 text-line animate-pulse' :
                  'bg-white/5 text-gray-600'
                }`}
              >
                {icons[step]}
                <div className="mt-1 capitalize">{step}</div>
              </div>
            )
          })}
        </div>
      </div>

      {/* Results */}
      {isDone && job.result && (
        <ResultsCard result={job.result} videoUrl={`/api/videos/${jobId}`} />
      )}

      {/* Failed */}
      {isFailed && (
        <div className="glass rounded-2xl p-8 text-center border-hard/30">
          <div className="text-4xl mb-4">💥</div>
          <h3 className="text-xl font-bold text-hard mb-2">Job Failed</h3>
          <p className="text-gray-400 text-sm">{job.error || 'Unknown error'}</p>
        </div>
      )}
    </div>
  )
}

function StatusBadge({ status }) {
  const config = {
    queued: { bg: 'bg-gray-500/20', text: 'text-gray-400', label: 'Queued' },
    fetching: { bg: 'bg-yellow-500/20', text: 'text-yellow-400', label: 'Fetching' },
    analysing: { bg: 'bg-blue-500/20', text: 'text-blue-400', label: 'Analysing' },
    rendering: { bg: 'bg-purple-500/20', text: 'text-purple-400', label: 'Rendering' },
    done: { bg: 'bg-accent/20', text: 'text-accent', label: 'Done' },
    failed: { bg: 'bg-hard/20', text: 'text-hard', label: 'Failed' },
  }
  const c = config[status] || config.queued
  return (
    <span className={`px-3 py-1 rounded-full text-xs font-medium ${c.bg} ${c.text}`}>
      {c.label}
    </span>
  )
}

function ResultsCard({ result, videoUrl }) {
  const s = result.summary || {}
  const tags = result.metadata || {}

  return (
    <div className="glass rounded-2xl p-8 space-y-8">
      {/* Verdict */}
      <div className="text-center">
        <div className="text-5xl mb-4">{getVerdictEmoji(s.rating)}</div>
        <h3 className="text-2xl font-bold bg-gradient-to-r from-[#ff1744] to-[#d500f9] bg-clip-text text-transparent">
          {s.rating || 'Unrated'}
        </h3>
      </div>

      {/* Stats Grid */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatBlock label="Hard Slurs" value={s.total_hard} color="hard" icon="🔴" />
        <StatBlock label="Soft Slurs" value={s.total_soft} color="soft" icon="🟡" />
        <StatBlock label="F-Bombs" value={s.total_f_bombs} color="fbomb" icon="💣" />
        <StatBlock label="Peak Score" value={s.peak_score} color="accent" icon="📈" />
      </div>

      {/* Additional Stats */}
      <div className="flex justify-between text-sm text-gray-400">
        <span>⏱️ Runtime: {s.runtime_minutes} min</span>
        <span>⏱️ Peak: Minute {s.peak_minute}</span>
        <span>🔢 Total hits: {s.total_words_counted}</span>
      </div>

      {/* Video Download */}
      {videoUrl && (
        <div className="text-center pt-4">
          <a
            href={videoUrl}
            download
            className="inline-flex items-center gap-2 px-8 py-3 bg-gradient-to-r from-[#ff1744] to-[#d500f9] rounded-xl font-bold text-lg hover:shadow-[0_0_30px_rgba(255,23,68,0.3)] transition-all"
          >
            📥 Download Video
          </a>
          <p className="text-xs text-gray-500 mt-2">9:16 Shorts-ready MP4</p>
        </div>
      )}
    </div>
  )
}

function StatBlock({ label, value, color, icon }) {
  return (
    <div className={`glass rounded-xl p-4 text-center`}>
      <div className="text-2xl mb-1">{icon}</div>
      <div className={`text-2xl font-bold text-${color}`}>{value}</div>
      <div className="text-xs text-gray-500 mt-1">{label}</div>
    </div>
  )
}

function getVerdictEmoji(rating) {
  if (!rating) return '📊'
  if (rating.includes('TOXIC') || rating.includes('HAZMAT')) return '💀'
  if (rating.includes('EDGY') || rating.includes('F-Bomb')) return '🔥'
  if (rating.includes('Spicy') || rating.includes('Mild')) return '😄'
  if (rating.includes('CLEAN') || rating.includes('ANGEL') || rating.includes('Wholesome')) return '⭐'
  return '📊'
}