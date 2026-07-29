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
    <section className="page" aria-labelledby="completed-heading">
      <header className="page-heading"><div>
        <h1 id="completed-heading">Completed videos</h1>
        <p className="lede">Every finished render, one card per movie. Filter and sort, then open one to watch or publish.</p>
      </div></header>
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
              <div className="gallery-filters">
                <label className="gallery-filters__search">
                  <span className="sr-only">Search completed movies</span>
                  <input
                    type="search"
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="Search by title or IMDb id…"
                    className="input"
                  />
                </label>
                <label className="gallery-filters__sort">
                  <span>Sort</span>
                  <select
                    value={sort}
                    onChange={(event) => setSort(event.target.value)}
                    className="input"
                  >
                    {Object.entries(SORTS).map(([key, option]) => (
                      <option key={key} value={key}>{option.label}</option>
                    ))}
                  </select>
                </label>
                <span className="micro data">{sorted.length} of {movies.length} movie{movies.length === 1 ? '' : 's'}</span>
              </div>

              {sorted.length ? (
                <ul className="gallery">
                  {sorted.map((movie) => (
                    <li key={movie.job_id}>
                      <Link to={`/jobs/${movie.job_id}`} className="gallery__card" aria-label={`Open ${movie.label}`}>
                        <CompletedThumb jobId={movie.job_id} client={client} />
                        <div className="gallery__meta">
                          <div className="gallery__title">
                            <strong className="capitalize">{movie.label}</strong>
                            {movie.rating && <span className="badge" data-metric="rating">{movie.rating}</span>}
                          </div>
                          {movie.source_imdb_id && <small className="micro data">{movie.source_imdb_id}</small>}
                          <div className="gallery__counts">
                            <span><b data-metric="hard">{num(movie.hard)}</b> hard</span>
                            <span><b data-metric="soft">{num(movie.soft)}</b> soft</span>
                            <span><b data-metric="f-bombs">{num(movie.f_bombs)}</b> f-bombs</span>
                          </div>
                          <div className="gallery__foot">
                            <span><b data-metric="views">{num(movie.total_views)}</b> views</span>
                            <span>{shortDate(movie.finished_at)}</span>
                          </div>
                        </div>
                      </Link>
                    </li>
                  ))}
                </ul>
              ) : <p className="state-block">No completed videos match your search.</p>}
            </>
          )
        }}
      </ResourceState>
    </section>
  )
}
