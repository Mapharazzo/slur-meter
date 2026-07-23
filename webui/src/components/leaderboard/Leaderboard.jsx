import { useCallback } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../../api'
import { useApp } from '../../context/AppContext'
import { usePollingResource } from '../../hooks/usePollingResource'
import ResourceState from '../shared/ResourceState'

function value(number) {
  return Number(number ?? 0).toLocaleString()
}

export default function Leaderboard({ client = api, pollingOptions = {} }) {
  const { operatorToken } = useApp()
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
    <section className="max-w-5xl mx-auto space-y-6" aria-labelledby="leaderboard-heading">
      <header>
        <h1 id="leaderboard-heading">Toxicity leaderboard</h1>
        <p>Persisted analysis metrics for completed runs.</p>
      </header>
      <ResourceState
        resource={resource}
        loadingMessage="Loading leaderboard…"
        emptyMessage="No completed runs have leaderboard data."
        isEmpty={(page) => !page?.items?.length}
      >
        {(page) => (
          <>
            <p>{page.total} ranked run{page.total === 1 ? '' : 's'}</p>
            <ol className="space-y-2">
              {page.items.map((item, index) => (
                <li key={item.job_id}>
                  <Link to={`/jobs/${item.job_id}`} className="glass flex items-center gap-4 rounded-xl p-4">
                    <span aria-label={`Rank ${index + 1}`}>{index + 1}</span>
                    <span className="flex-1">
                      <strong>{item.label}</strong>
                      {item.source_imdb_id && <small className="block">{item.source_imdb_id}</small>}
                    </span>
                    <span><b data-metric="hard">{value(item.hard)}</b> hard</span>
                    <span><b data-metric="soft">{value(item.soft)}</b> soft</span>
                    <span><b data-metric="f-bombs">{value(item.f_bombs)}</b> f-bombs</span>
                    <span><b data-metric="views">{value(item.total_views)}</b> views</span>
                  </Link>
                </li>
              ))}
            </ol>
          </>
        )}
      </ResourceState>
    </section>
  )
}
