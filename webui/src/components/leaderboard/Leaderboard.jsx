import { useState, useEffect, useCallback } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../../api'

const PLATFORMS = [
  { key: 'youtube',   label: 'YouTube',   color: 'text-red-400',    bg: 'bg-red-500/10'    },
  { key: 'tiktok',    label: 'TikTok',    color: 'text-pink-400',   bg: 'bg-pink-500/10'   },
  { key: 'instagram', label: 'Instagram', color: 'text-purple-400', bg: 'bg-purple-500/10' },
]

function fmt(n) {
  if (n == null || n === 0) return '—'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return n.toString()
}

function fmtUsd(n) {
  if (!n) return '—'
  return `$${n.toFixed(2)}`
}

function StatusDot({ status }) {
  const styles = {
    uploaded: 'bg-green-400',
    pending:  'bg-yellow-400 animate-pulse',
    failed:   'bg-red-400',
  }
  return (
    <span className={`inline-block w-1.5 h-1.5 rounded-full ${styles[status] ?? 'bg-gray-600'}`} />
  )
}

function PlatformBadge({ platform, info }) {
  const { label, color, bg } = platform
  const published = info?.status === 'uploaded'
  const url = info?.platform_id
    ? platform.key === 'youtube'
      ? `https://www.youtube.com/shorts/${info.platform_id}`
      : platform.key === 'tiktok'
      ? `https://www.tiktok.com/video/${info.platform_id}`
      : `https://www.instagram.com/p/${info.platform_id}/`
    : null

  const inner = (
    <div className={`rounded-lg px-3 py-2 ${bg} flex flex-col gap-0.5 min-w-[90px]`}>
      <div className={`flex items-center gap-1.5 text-xs font-semibold ${color}`}>
        {info && <StatusDot status={info.status} />}
        {label}
      </div>
      {published ? (
        <div className="grid grid-cols-2 gap-x-3 text-[11px] text-gray-400 mt-0.5">
          <span>{fmt(info.views)} views</span>
          <span>{fmt(info.likes)} likes</span>
          <span>{fmt(info.comments)} cmt</span>
          <span>{fmtUsd(info.revenue_usd)}</span>
        </div>
      ) : (
        <div className="text-[11px] text-gray-600 mt-0.5">
          {info?.status ?? 'not published'}
        </div>
      )}
    </div>
  )

  if (url) {
    return (
      <a href={url} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()}>
        {inner}
      </a>
    )
  }
  return inner
}

function RefreshButton({ imdbId, onDone }) {
  const [loading, setLoading] = useState(false)

  async function handleRefresh(e) {
    e.preventDefault()
    e.stopPropagation()
    setLoading(true)
    try {
      await api.refreshStats(imdbId)
      // Give the background task a moment then reload
      setTimeout(onDone, 3_000)
    } catch (_) {
      // no-op: e.g. no uploads yet
    } finally {
      setLoading(false)
    }
  }

  return (
    <button
      onClick={handleRefresh}
      disabled={loading}
      className="text-[11px] text-gray-600 hover:text-gray-400 transition-colors disabled:opacity-40 shrink-0"
      title="Refresh platform stats"
    >
      {loading ? '⟳' : '↺'}
    </button>
  )
}

export default function Leaderboard() {
  const [movies, setMovies] = useState([])

  const load = useCallback(async () => {
    try {
      const data = await api.getLeaderboard()
      setMovies(data)
    } catch (_) {}
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, 15_000)
    return () => clearInterval(id)
  }, [load])

  const totalViews = movies.reduce((s, m) => s + (m.total_views ?? 0), 0)

  return (
    <div className="max-w-5xl mx-auto space-y-6">
      <div className="text-center space-y-1">
        <h2 className="text-2xl font-bold">Toxicity Leaderboard</h2>
        <p className="text-gray-500 text-sm">
          Ranked by hard slurs + f-bombs
          {totalViews > 0 && (
            <span className="ml-3 text-gray-600">· {fmt(totalViews)} total views</span>
          )}
        </p>
      </div>

      {movies.length === 0 ? (
        <div className="glass rounded-xl p-8 text-center text-gray-600">
          No completed jobs yet
        </div>
      ) : (
        <div className="space-y-2">
          {movies.map((movie, i) => (
            <div key={movie.imdb_id} className="glass rounded-xl overflow-hidden">
              {/* Main row */}
              <Link
                to={`/jobs/${movie.imdb_id}`}
                className="flex items-center gap-4 p-4 hover:bg-white/[0.04] transition-all"
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

                {/* Title */}
                <div className="flex-1 min-w-0">
                  <div className="font-bold truncate">{movie.label}</div>
                  <div className="text-xs text-gray-600">{movie.rating}</div>
                </div>

                {/* Toxicity stats */}
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
                  {movie.total_views > 0 && (
                    <div className="text-right">
                      <div className="text-blue-400 font-mono font-bold">{fmt(movie.total_views)}</div>
                      <div className="text-xs text-gray-600">views</div>
                    </div>
                  )}
                </div>

                <RefreshButton imdbId={movie.imdb_id} onDone={load} />
              </Link>

              {/* Platform breakdown */}
              <div className="flex gap-2 px-4 pb-3 flex-wrap">
                {PLATFORMS.map(plat => (
                  <PlatformBadge
                    key={plat.key}
                    platform={plat}
                    info={movie.platforms?.[plat.key] ?? null}
                  />
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
