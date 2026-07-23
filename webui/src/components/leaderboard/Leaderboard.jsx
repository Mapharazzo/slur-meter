import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../../api'
import CompletedThumb from '../completed/CompletedThumb'
import { useApp } from '../../context/AppContext'
import { usePollingResource } from '../../hooks/usePollingResource'
import ResourceState from '../shared/ResourceState'

const SORTS = {
  recent: { label: 'Recently completed', compare: (a, b) => String(b.finished_at || '').localeCompare(String(a.finished_at || '')) },
  f_bombs: { label: 'Most f-bombs', compare: (a, b) => b.f_bombs - a.f_bombs },
  hard: { label: 'Most hard slurs', compare: (a, b) => b.hard - a.hard },
  views: { label: 'Most views', compare: (a, b) => b.total_views - a.total_views },
  az: { label: 'A → Z', compare: (a, b) => a.label.localeCompare(b.label) },
}

// One card per movie: hash by IMDb id when present, else the normalised title.
function movieKey(item) {
  return item.source_imdb_id || item.label.trim().toLowerCase()
}

function dedupeByMovie(items) {
  const seen = new Map()
  for (const item of items) {
    // `items` arrive newest-first, so the first per key is the latest render.
    const key = movieKey(item)
    if (!seen.has(key)) seen.set(key, item)
  }
  return [...seen.values()]
}

function num(value) {
  return Number(value ?? 0).toLocaleString()
}

function shortDate(value) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

export default function Leaderboard({ client = api, pollingOptions = {} }) {
  const { operatorToken } = useApp()
  const [query, setQuery] = useState('')
  const [sort, setSort] = useState('recent')
  const load = useCallback(
    (signal) => client.getLeaderboard({ token: operatorToken, signal }),
    [client, operatorToken],
  )
  const resource = usePollingResource(load, {
    intervalMs: 15_000,
    staleAfterMs: 45_000,
    dependencies: [operatorToken],
    ...pollingOptions,
  })

  return (
    <section className="max-w-6xl mx-auto space-y-6" aria-labelledby="completed-heading">
      <header className="space-y-1">
        <h1 id="completed-heading">Completed videos</h1>
        <p>Every finished render, one card per movie. Filter and sort, then open one to watch or publish.</p>
      </header>
      <ResourceState
        resource={resource}
        loadingMessage="Loading completed videos…"
        emptyMessage="No completed videos yet."
        isEmpty={(page) => !page?.items?.length}
      >
        {(page) => {
          const movies = dedupeByMovie(page.items)
          const needle = query.trim().toLowerCase()
          const filtered = needle
            ? movies.filter((movie) => movie.label.toLowerCase().includes(needle) || String(movie.source_imdb_id || '').toLowerCase().includes(needle))
            : movies
          const sorted = [...filtered].sort(SORTS[sort].compare)
          return (
            <>
              <div className="flex flex-wrap items-center gap-3">
                <label className="min-w-[220px] flex-1">
                  <span className="sr-only">Search completed movies</span>
                  <input
                    type="search"
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="Search by title or IMDb id…"
                    className="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2"
                  />
                </label>
                <label className="flex items-center gap-2 text-sm">
                  <span>Sort</span>
                  <select
                    value={sort}
                    onChange={(event) => setSort(event.target.value)}
                    className="rounded-lg border border-white/10 bg-white/5 px-3 py-2"
                  >
                    {Object.entries(SORTS).map(([key, option]) => (
                      <option key={key} value={key}>{option.label}</option>
                    ))}
                  </select>
                </label>
                <span className="text-sm opacity-70">{sorted.length} of {movies.length} movie{movies.length === 1 ? '' : 's'}</span>
              </div>

              {sorted.length ? (
                <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                  {sorted.map((movie) => (
                    <li key={movie.job_id}>
                      <Link to={`/jobs/${movie.job_id}`} className="glass block overflow-hidden rounded-2xl transition hover:ring-1 hover:ring-white/20" aria-label={`Open ${movie.label}`}>
                        <CompletedThumb jobId={movie.job_id} client={client} />
                        <div className="space-y-2 p-4">
                          <div className="flex items-start justify-between gap-2">
                            <strong className="capitalize">{movie.label}</strong>
                            {movie.rating && <span className="shrink-0 rounded-full bg-white/10 px-2 py-0.5 text-xs" data-metric="rating">{movie.rating}</span>}
                          </div>
                          {movie.source_imdb_id && <small className="block opacity-60">{movie.source_imdb_id}</small>}
                          <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
                            <span><b data-metric="hard">{num(movie.hard)}</b> hard</span>
                            <span><b data-metric="soft">{num(movie.soft)}</b> soft</span>
                            <span><b data-metric="f-bombs">{num(movie.f_bombs)}</b> f-bombs</span>
                          </div>
                          <div className="flex flex-wrap justify-between gap-2 text-xs opacity-70">
                            <span><b data-metric="views">{num(movie.total_views)}</b> views</span>
                            <span>{shortDate(movie.finished_at)}</span>
                          </div>
                        </div>
                      </Link>
                    </li>
                  ))}
                </ul>
              ) : <p>No completed videos match your search.</p>}
            </>
          )
        }}
      </ResourceState>
    </section>
  )
}
