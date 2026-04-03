import React, { useState, useEffect } from 'react'
import ReactDOM from 'react-dom/client'
import './index.css'

const API = '/api'

function App() {
  const [view, setView] = useState('dashboard')
  const [selectedJob, setSelectedJob] = useState(null)
  const [jobs, setJobs] = useState([])
  const [submitForm, setSubmitForm] = useState({ mode: 'query', input: '' })
  const [submitting, setSubmitting] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)

  // Poll jobs every 2s
  useEffect(() => {
    const poll = async () => {
      try {
        const res = await fetch(`${API}/jobs`)
        if (res.ok) setJobs(await res.json())
      } catch (e) {}
    }
    poll()
    const id = setInterval(poll, 2000)
    return () => clearInterval(id)
  }, [refreshKey])

  // Poll selected job in detail
  const selectedJobData = jobs.find(j => j.id === selectedJob)

  const handleSubmitMovie = async (e) => {
    e.preventDefault()
    if (!submitForm.input.trim() || submitting) return
    setSubmitting(true)

    try {
      const body = submitForm.mode === 'imdb'
        ? { imdb_id: submitForm.input.trim() }
        : { query: submitForm.input.trim() }

      const res = await fetch(`${API}/jobs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || 'Failed')
      }
      const job = await res.json()
      setSelectedJob(job.id)
      setView('job')
      setSubmitForm({ mode: 'query', input: '' })
    } catch (err) {
      alert('❌ ' + err.message)
    } finally {
      setSubmitting(false)
    }
  }

  // ─── Dashboard ───
  if (view === 'dashboard') {
    const inProgress = jobs.filter(j =>
      ['queued', 'fetching', 'analysing', 'rendering'].includes(j.status)
    )
    const done = jobs.filter(j => j.status === 'done')

    return (
      <div className="min-h-screen">
        <Header tab="dashboard" setTab={setView} />
        <div className="max-w-4xl mx-auto px-4 py-8 space-y-8">
          {/* Stats */}
          <div className="grid grid-cols-4 gap-4">
            <StatCard icon="📊" value={jobs.length} label="Total" />
            <StatCard icon="⏳" value={inProgress.length} label="Running" />
            <StatCard icon="✅" value={done.length} label="Done" />
            <StatCard icon="💥" value={jobs.filter(j => j.status === 'failed').length} label="Failed" />
          </div>

          {/* Submit CTA */}
          <div className="glass rounded-2xl p-8 text-center space-y-4">
            <h2 className="text-2xl font-bold">🎬 Analyse a new movie</h2>
            <p className="text-gray-400 text-sm">Enter IMDB ID or movie title</p>

            <div className="max-w-md mx-auto">
              <div className="flex gap-2 mb-3">
                <button
                  onClick={() => setSubmitForm(m => ({...m, mode: 'query'}))}
                  className={`flex-1 py-2 rounded-lg text-sm font-medium transition-all ${
                    submitForm.mode === 'query' ? 'bg-white/10 text-white' : 'text-gray-500'
                  }`}
                >
                  Search
                </button>
                <button
                  onClick={() => setSubmitForm(m => ({...m, mode: 'imdb'}))}
                  className={`flex-1 py-2 rounded-lg text-sm font-medium transition-all ${
                    submitForm.mode === 'imdb' ? 'bg-white/10 text-white' : 'text-gray-500'
                  }`}
                >
                  IMDB ID
                </button>
              </div>

              <form onSubmit={handleSubmitMovie} className="flex gap-2">
                <input
                  type="text"
                  value={submitForm.input}
                  onChange={e => setSubmitForm(m => ({...m, input: e.target.value}))}
                  placeholder={submitForm.mode === 'query' ? 'e.g. Django Unchained' : 'e.g. tt1854564'}
                  className="flex-1 bg-white/5 border border-white/10 rounded-xl px-4 py-3 focus:outline-none focus:border-accent transition-colors"
                />
                <button
                  type="submit"
                  disabled={submitting || !submitForm.input.trim()}
                  className="px-6 py-3 bg-gradient-to-r from-hard to-fbomb rounded-xl font-bold hover:glow-hard transition-all disabled:opacity-40"
                >
                  {submitting ? '⏳' : '🚀'}
                </button>
              </form>

              {/* Quick picks */}
              <div className="flex flex-wrap gap-2 mt-4 justify-center">
                {['Django Unchained 2012', 'Pulp Fiction 1994', 'The Wolf of Wall Street 2013', 'Goodfellas 1990', 'Casino 1995'].map(movie => (
                  <button
                    key={movie}
                    onClick={() => setSubmitForm({ mode: 'query', input: movie })}
                    className="px-3 py-1 rounded-full bg-white/5 text-xs text-gray-400 hover:text-white hover:bg-white/10 transition-all"
                  >
                    {movie}
                  </button>
                ))}
              </div>
            </div>
          </div>

          {/* Jobs List */}
          <div className="space-y-3">
            <h3 className="font-bold text-gray-400 text-sm uppercase tracking-wider">Recent</h3>
            {jobs.length === 0 && (
              <p className="text-center text-gray-600 py-8">No jobs yet</p>
            )}
            {jobs.map(job => (
              <button
                key={job.id}
                onClick={() => { setSelectedJob(job.id); setView('job') }}
                className="w-full glass rounded-xl p-4 flex items-center gap-4 hover:bg-white/[0.04] text-left transition-all"
              >
                <StatusDot status={job.status} />
                <div className="flex-1 min-w-0">
                  <div className="font-semibold truncate">{job.label || job.query || job.imdb_id}</div>
                  <div className="text-xs text-gray-600 mt-1">{job.id} • {new Date(job.created_at).toLocaleTimeString()}</div>
                </div>
                {job.status === 'done' && job.result && (
                  <div className="text-right hidden sm:block">
                    <div className="text-sm font-mono">
                      🔴<span className="text-hard">{job.result.summary?.total_hard || 0}</span>
                      {' '}💣<span className="text-fbomb">{job.result.summary?.total_f_bombs || 0}</span>
                    </div>
                    <div className="text-xs text-accent mt-1">{job.result.summary?.rating}</div>
                  </div>
                )}
                {['queued', 'fetching', 'analysing', 'rendering'].includes(job.status) && (
                  <span className="text-xs text-gray-400 animate-pulse-slow hidden sm:block">{job.message}</span>
                )}
                {job.status === 'failed' && (
                  <span className="text-hard text-sm">💥 {job.error || 'Failed'}</span>
                )}
              </button>
            ))}
          </div>
        </div>
      </div>
    )
  }

  // ─── Job Detail ───
  if (view === 'job' && selectedJobData) {
    const job = selectedJobData
    const isActive = ['queued', 'fetching', 'analysing', 'rendering'].includes(job.status)
    const isDone = job.status === 'done'
    const isFailed = job.status === 'failed'
    const s = isDone ? job.result?.summary : {}

    return (
      <div className="min-h-screen">
        <Header tab="job" setTab={setView} />
        <div className="max-w-3xl mx-auto px-4 py-8 space-y-6">
          <button onClick={() => setView('dashboard')} className="text-gray-500 hover:text-white text-sm">← Back</button>

          {/* Progress */}
          <div className="glass rounded-2xl p-6 space-y-4">
            <div className="flex items-center gap-3">
              <StatusDot status={job.status} />
              <h2 className="text-xl font-bold">{job.label || 'Processing…'}</h2>
            </div>
            <div className="w-full bg-white/5 rounded-full h-2">
              <div
                className="h-2 rounded-full bg-gradient-to-r from-hard to-fbomb transition-all duration-1000"
                style={{ width: `${job.progress || 0}%` }}
              />
            </div>
            <p className="text-sm text-gray-400">{job.message}</p>

            {/* Pipeline Steps */}
            <div className="grid grid-cols-5 gap-2 text-center text-xs">
              {[
                { key: 'queued', icon: '📋' },
                { key: 'fetching', icon: '📥' },
                { key: 'analysing', icon: '🔍' },
                { key: 'rendering', icon: '🎬' },
                { key: 'done', icon: '✅' },
              ].map((step, i) => {
                const steps = ['queued', 'fetching', 'analysing', 'rendering', 'done']
                const curIdx = steps.indexOf(job.status)
                const doneStep = curIdx > i || isDone
                const currentStep = curIdx === i && isActive
                return (
                  <div key={step.key}
                    className={`py-2 rounded-lg transition-all ${
                      doneStep ? 'bg-accent/10 text-accent' :
                      currentStep ? 'bg-line/10 text-line animate-pulse-slow' :
                      'bg-white/5 text-gray-700'
                    }`}
                  >
                    <span className="text-lg">{step.icon}</span>
                    <div className="mt-1 capitalize">{step.key}</div>
                  </div>
                )
              })}
            </div>
          </div>

          {/* Results */}
          {isDone && s && (
            <div className="glass rounded-2xl p-8 space-y-8">
              <div className="text-center">
                <div className="text-6xl mb-3">
                  {(s.total_hard > 100 || s.total_f_bombs > 200) ? '💀' :
                   s.total_hard > 0 ? '🔥' :
                   s.total_f_bombs > 0 ? '😬' : '🌸'}
                </div>
                <h3 className="text-2xl font-bold gradient-text">{s.rating || 'Unrated'}</h3>
              </div>

              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <StatBlock label="Hard Slurs" value={s.total_hard} color="hard" icon="🔴" />
                <StatBlock label="Soft Slurs" value={s.total_soft} color="soft" icon="🟡" />
                <StatBlock label="F-Bombs" value={s.total_f_bombs} color="fbomb" icon="💣" />
                <StatBlock label="Peak Score" value={s.peak_score} color="accent" icon="📈" />
              </div>

              <div className="flex justify-between text-sm text-gray-500">
                <span>⏱️ {s.runtime_minutes} min</span>
                <span>📈 Peak: min {s.peak_minute}</span>
                <span>🔢 {s.total_words_counted} hits</span>
              </div>

              {job.video_url && (
                <a
                  href={job.video_url}
                  className="block text-center px-8 py-4 bg-gradient-to-r from-hard to-fbomb rounded-xl font-bold text-lg hover:glow-hard transition-all"
                >
                  📥 Download 9:16 MP4
                </a>
              )}
            </div>
          )}

          {isFailed && (
            <div className="glass rounded-2xl p-8 text-center border-hard/30">
              <div className="text-4xl mb-3">💥</div>
              <h3 className="text-xl font-bold text-hard">Failed</h3>
              <p className="text-gray-400 mt-2 text-sm">{job.error || 'Unknown error'}</p>
            </div>
          )}
        </div>
      </div>
    )
  }

  return <div className="min-h-screen"><Header tab="dashboard" setTab={setView} /></div>
}

// ─── Shared Components ───

function Header({ tab, setTab }) {
  return (
    <header className="border-b border-white/5 bg-[#0d0d0d]/80 backdrop-blur-md sticky top-0 z-50">
      <div className="max-w-6xl mx-auto px-4 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3 cursor-pointer" onClick={() => setTab('dashboard')}>
          <span className="text-3xl">📉</span>
          <h1 className="text-xl font-bold bg-gradient-to-r from-hard to-fbomb bg-clip-text text-transparent">
            Daily Slur Meter
          </h1>
        </div>
        <nav className="flex gap-1">
          {[
            { key: 'dashboard', label: '📊 Dashboard' },
            { key: 'leaderboard', label: '🏆 Ranking' },
          ].map(t => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`px-4 py-2 rounded-lg text-sm font-medium transition-all ${
                tab === t.key ? 'bg-white/10' : 'text-gray-500 hover:text-white'
              }`}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </div>
    </header>
  )
}

function StatCard({ icon, value, label }) {
  return (
    <div className="glass rounded-xl p-4 text-center">
      <div className="text-2xl mb-1">{icon}</div>
      <div className="text-2xl font-bold text-white">{value}</div>
      <div className="text-xs text-gray-500 mt-1">{label}</div>
    </div>
  )
}

function StatBlock({ label, value, color, icon }) {
  return (
    <div className="glass rounded-xl p-4 text-center space-y-1">
      <div className="text-2xl">{icon}</div>
      <div className={`text-2xl font-bold text-${color}`}>{value}</div>
      <div className="text-xs text-gray-500">{label}</div>
    </div>
  )
}

function StatusDot({ status }) {
  const c = {
    queued: 'bg-gray-500',
    fetching: 'bg-yellow-500',
    analysing: 'bg-blue-500',
    rendering: 'bg-purple-500',
    done: 'bg-accent',
    failed: 'bg-hard',
  }[status] || 'bg-gray-500'
  return <div className={`w-3 h-3 rounded-full ${c}`} />
}

export default App