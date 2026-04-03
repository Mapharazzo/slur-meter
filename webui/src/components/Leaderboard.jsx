import React, { useState, useEffect } from 'react'

const API = '/api'

export default function Leaderboard() {
  const [movies, setMovies] = useState([])

  useEffect(() => {
    const fetch = async () => {
      try {
        const res = await fetch(`${API}/leaderboard`)
        const data = await res.json()
        setMovies(data)
      } catch (e) {
        //
      }
    }
    fetch()
    const interval = setInterval(fetch, 5000)
    return () => clearInterval(interval)
  }, [])

  return (
    <div className="max-w-4xl mx-auto space-y-8">
      <div className="text-center">
        <h2 className="text-3xl font-bold mb-2">🏆 Toxicity Leaderboard</h2>
        <p className="text-gray-400">Ranked by total hard slurs + f-bombs</p>
      </div>

      {movies.length === 0 ? (
        <div className="glass rounded-xl p-8 text-center text-gray-500">
          No movies analysed yet!
        </div>
      ) : (
        <div className="space-y-3">
          {movies.map((movie, i) => (
            <div
              key={movie.id}
              className="glass rounded-xl p-4 flex items-center gap-4"
            >
              {/* Rank */}
              <div className={`w-10 h-10 rounded-full flex items-center justify-center font-bold text-lg ${
                i === 0 ? 'bg-yellow-500/20 text-yellow-400' :
                i === 1 ? 'bg-gray-400/20 text-gray-300' :
                i === 2 ? 'bg-orange-600/20 text-orange-400' :
                'bg-white/5 text-gray-500'
              }`}>
                {i + 1}
              </div>

              {/* Movie Info */}
              <div className="flex-1">
                <div className="font-bold text-white">{movie.label}</div>
                <div className="text-xs text-gray-500 mt-1">{movie.rating}</div>
              </div>

              {/* Stats */}
              <div className="flex gap-6 text-sm">
                <div className="text-right">
                  <div className="text-hard font-mono font-bold">🔴 {movie.hard}</div>
                </div>
                <div className="text-right">
                  <div className="text-fbomb font-mono font-bold">💣 {movie.f_bombs}</div>
                </div>
                <div className="text-right">
                  <div className="text-accent font-mono font-bold">📈 {movie.peak_score}</div>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}