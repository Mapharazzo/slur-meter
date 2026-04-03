import React, { useState } from 'react'

const API = '/api'

export default function JobSubmit({ onSubmitted, onBack }) {
  const [mode, setMode] = useState('query') // query | imdb
  const [input, setInput] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!input.trim()) return

    setSubmitting(true)
    setError('')

    try {
      const body = mode === 'imdb'
        ? { imdb_id: input.trim() }
        : { query: input.trim() }

      const res = await fetch(`${API}/jobs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })

      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || 'Submission failed')
      }

      const job = await res.json()
      onSubmitted(job.id)
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  const quickPicks = [
    { label: "Pulp Fiction", query: "Pulp Fiction 1994" },
    { label: "The Wolf of Wall Street", query: "The Wolf of Wall Street 2013" },
    { label: "Goodfellas", query: "Goodfellas 1990" },
    { label: "Django Unchained", query: "Django Unchained 2012" },
    { label: "Casino", query: "Casino 1995" },
  ]

  return (
    <div className="max-w-2xl mx-auto space-y-8">
      {/* Back */}
      <button
        onClick={onBack}
        className="text-gray-500 hover:text-white transition-colors"
      >
        ← Back to Dashboard
      </button>

      {/* Header */}
      <div className="text-center">
        <h2 className="text-3xl font-bold mb-2">🎬 Submit a Movie</h2>
        <p className="text-gray-400">
          Enter an IMDB ID or search by title
        </p>
      </div>

      {/* Mode Toggle */}
      <div className="flex justify-center gap-4">
        <button
          onClick={() => setMode('query')}
          className={`px-6 py-2 rounded-lg font-medium transition-all ${
            mode === 'query'
              ? 'bg-white/10 text-white'
              : 'text-gray-500 hover:text-white'
          }`}
        >
          Search by Title
        </button>
        <button
          onClick={() => setMode('imdb')}
          className={`px-6 py-2 rounded-lg font-medium transition-all ${
            mode === 'imdb'
              ? 'bg-white/10 text-white'
              : 'text-gray-500 hover:text-white'
          }`}
        >
          IMBD ID
        </button>
      </div>

      {/* Form */}
      <form onSubmit={handleSubmit} className="glass rounded-2xl p-8 space-y-4">
        <div>
          <label className="block text-sm font-medium text-gray-300 mb-2">
            {mode === 'query' ? 'Movie Title' : 'IMDB ID'}
          </label>
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={mode === 'query'
              ? 'e.g. Pulp Fiction'
              : 'e.g. tt0110912'}
            className="w-full bg-white/5 border border-white/10 rounded-xl px-4 py-3 text-lg focus:outline-none focus:border-accent transition-colors"
          />
        </div>

        {error && (
          <div className="text-hard text-sm bg-hard/10 rounded-lg p-3">
            💥 {error}
          </div>
        )}

        <button
          type="submit"
          disabled={submitting || !input.trim()}
          className={`w-full py-3 rounded-xl font-bold text-lg transition-all ${
            submitting || !input.trim()
              ? 'bg-gray-800 text-gray-600 cursor-not-allowed'
              : 'bg-gradient-to-r from-[#ff1744] to-[#d500f9] hover:shadow-[0_0_30px_rgba(255,23,68,0.3)]'
          }`}
        >
          {submitting ? '⏳ Submitting…' : '🚀 Analyse Movie'}
        </button>
      </form>

      {/* Quick Picks */}
      <div className="space-y-3">
        <h3 className="text-sm font-medium text-gray-500 uppercase tracking-wide">
          Quick Picks (Notorious)
        </h3>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
          {quickPicks.map(movie => (
            <button
              key={movie.label}
              onClick={() => { setMode('query'); setInput(movie.query); }}
              className="glass rounded-xl p-3 text-left hover:bg-white/5 transition-all text-sm"
            >
              <span className="block text-white font-medium">{movie.label}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}