import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../../api'

export default function Leaderboard() {
  const [movies, setMovies] = useState([])

  useEffect(() => {
    const loadLeaderboard = async () => {
      try {
        const data = await api.getLeaderboard()
        setMovies(data)
      } catch (e) { /* ignore */ }
    }
    loadLeaderboard()
    const id = setInterval(loadLeaderboard, 10000)
    return () => clearInterval(id)
  }, [])

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <div className="text-center space-y-1">
        <h2 className="text-2xl font-bold">Toxicity Leaderboard</h2>
        <p className="text-gray-500 text-sm">Ranked by hard slurs + f-bombs</p>
      </div>

      {movies.length === 0 ? (
        <div className="glass rounded-xl p-8 text-center text-gray-600">
          No completed jobs yet
        </div>
      ) : (
        <div className="space-y-2">
          {movies.map((movie, i) => (
            <Link
              key={movie.imdb_id}
              to={`/jobs/${movie.imdb_id}`}
              className="flex items-center gap-4 glass rounded-xl p-4 hover:bg-white/[0.04] transition-all"
            >
              {/* Rank */}
              <div className={`w-9 h-9 rounded-full flex items-center justify-center font-bold text-sm shrink-0 ${
                i === 0 ? 'bg-yellow-500/20 text-yellow-400' :
                i === 1 ? 'bg-gray-400/20 text-gray-300' :
                i === 2 ? 'bg-orange-600/20 text-orange-400' :
                'bg-white/5 text-gray-600'
              }`}>
                {i + 1}
              </div>

              <div className="flex-1 min-w-0">
                <div className="font-bold truncate">{movie.label}</div>
                <div className="text-xs text-gray-600">{movie.rating}</div>
              </div>

              <div className="flex gap-5 text-sm shrink-0">
                <div className="text-right">
                  <div className="text-red-400 font-mono font-bold">{movie.hard}</div>
                  <div className="text-xs text-gray-600">hard</div>
                </div>
                <div className="text-right">
                  <div className="text-purple-400 font-mono font-bold">{movie.f_bombs}</div>
                  <div className="text-xs text-gray-600">f-bombs</div>
                </div>
                <div className="text-right">
                  <div className="text-green-400 font-mono font-bold">{movie.peak_score}</div>
                  <div className="text-xs text-gray-600">peak</div>
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}
