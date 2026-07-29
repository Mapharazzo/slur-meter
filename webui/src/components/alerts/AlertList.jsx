import { useCallback } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../../api'
import { useApp } from '../../context/AppContext'
import { usePollingResource } from '../../hooks/usePollingResource'
import ResourceState from '../shared/ResourceState'

function timestamp(value) {
  return Number.isFinite(Date.parse(value)) ? value : null
}

export default function AlertList({ client = api, pollingOptions = {} }) {
  const { operatorToken } = useApp()
  const load = useCallback(
    (signal) => client.getAlerts(100, { token: operatorToken, signal }),
    [client, operatorToken],
  )
  const resource = usePollingResource(load, {
    intervalMs: 5_000,
    staleAfterMs: 20_000,
    dependencies: [operatorToken],
    ...pollingOptions,
  })

  return (
    <section className="page page--narrow" aria-labelledby="alerts-heading">
      <header className="page-heading"><div><p className="eyebrow">Waiting on you</p><h1 id="alerts-heading">Alerts</h1></div></header>
      <ResourceState
        resource={resource}
        loadingMessage="Loading alerts…"
        emptyMessage="No runs currently need operator attention."
        isEmpty={(page) => !page?.items?.length}
      >
        {(page) => (
          <>
            <p className="micro data">{page.total} need attention</p>
            <ul className="card-list">{page.items.map((alert) => {
              const validTimestamp = timestamp(alert.created_at)
              return (
                <li key={`${alert.job_id}:${alert.state}:${alert.created_at}`}>
                  <Link
                    to={`/jobs/${alert.job_id}`}
                    aria-label={`${alert.job_id} ${alert.state}: ${alert.message}`}
                    className="alert-card"
                  >
                    <strong>{alert.job_id}</strong>
                    <p className="micro">{alert.state}</p>
                    <p>{alert.message}</p>
                    {validTimestamp
                      ? <time dateTime={validTimestamp}>{validTimestamp}</time>
                      : <span>Invalid timestamp</span>}
                  </Link>
                </li>
              )
            })}</ul>
          </>
        )}
      </ResourceState>
    </section>
  )
}
