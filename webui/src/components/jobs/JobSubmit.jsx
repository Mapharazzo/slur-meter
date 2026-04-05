import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../../api'

const QUICK_PICKS = [
  'Django Unchained 2012',
  'Pulp Fiction 1994',
  'The Wolf of Wall Street 2013',
  'Goodfellas 1990',
  'Casino 1995',
]

export default function JobSubmit() {
  const [mode, setMode] = useState('query')
  const [input, setInput] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const navigate = useNavigate()

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!input.trim() || submitting) return
    setSubmitting(true)

    try {
      const body = mode === 'imdb'
        ? { imdb_id: input.trim() }
        : { query: input.trim() }
      const job = await api.submitJob(body)
      setInput('')
      navigate(`/jobs/${job.imdb_id}`)
    } catch (err) {
      alert(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="glass rounded-2xl p-8 space-y-4">
      <h2 className="text-lg font-bold text-center">New Video</h2>
      <p className="text-gray-500 text-xs text-center">Enter IMDB ID or movie title to start the pipeline</p>

      <div className="max-w-md mx-auto">
        <div className="flex gap-2 mb-3">
          {['query', 'imdb'].map(m => (
            <button
              key={m}
              onClick={() => setMode(m)}
              className={`flex-1 py-2 rounded-lg text-sm font-medium transition-all ${
                mode === m ? 'bg-white/10 text-white' : 'text-gray-500'
              }`}
            >
              {m === 'query' ? 'Search' : 'IMDB ID'}
            </button>
          ))}
        </div>

        <form onSubmit={handleSubmit} className="flex gap-2">
          <input
            type="text"
            value={input}
            onChange={e => setInput(e.target.value)}
            placeholder={mode === 'query' ? 'e.g. Django Unchained' : 'e.g. tt1854564'}
            className="flex-1 bg-white/5 border border-white/10 rounded-xl px-4 py-3 text-sm focus:outline-none focus:border-white/20 transition-colors"
          />
          <button
            type="submit"
            disabled={submitting || !input.trim()}
            className="px-6 py-3 bg-gradient-to-r from-red-500 to-purple-600 rounded-xl font-bold text-sm hover:opacity-90 transition-all disabled:opacity-40"
          >
            {submitting ? '...' : 'Go'}
          </button>
        </form>

        <div className="flex flex-wrap gap-2 mt-3 justify-center">
          {QUICK_PICKS.map(movie => (
            <button
              key={movie}
              onClick={() => { setMode('query'); setInput(movie) }}
              className="px-3 py-1 rounded-full bg-white/5 text-xs text-gray-500 hover:text-white hover:bg-white/10 transition-all"
            >
              {movie}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
