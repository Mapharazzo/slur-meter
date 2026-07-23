import { useCallback } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../../api'
import { useApp } from '../../context/AppContext'
import { usePollingResource } from '../../hooks/usePollingResource'
import ResourceState from '../shared/ResourceState'

export default function RevenueDashboard({ client = api, pollingOptions = {} }) {
  const { operatorToken } = useApp()
  const load = useCallback(
    (signal) => client.getRevenue(undefined, { token: operatorToken, signal }),
    [client, operatorToken],
  )
  const resource = usePollingResource(load, {
    intervalMs: 30_000,
    staleAfterMs: 90_000,
    dependencies: [operatorToken],
    ...pollingOptions,
  })

  return (
    <section className="max-w-5xl mx-auto space-y-6" aria-labelledby="revenue-heading">
      <header><h1 id="revenue-heading">Revenue and views</h1></header>
      <ResourceState
        resource={resource}
        loadingMessage="Loading revenue snapshots…"
        emptyMessage="No revenue snapshots have been persisted."
        isEmpty={(page) => !page?.items?.length}
      >
        {(page) => {
          const totalRevenue = page.items.reduce((sum, item) => sum + Number(item.revenue_usd ?? 0), 0)
          const totalViews = page.items.reduce((sum, item) => sum + Number(item.views ?? 0), 0)
          return (
            <>
              <p><strong>${totalRevenue.toFixed(2)}</strong> verified revenue</p>
              <p><strong>{totalViews.toLocaleString()} total views</strong> across {page.total} snapshot{page.total === 1 ? '' : 's'}</p>
              <div className="overflow-x-auto">
                <table>
                  <caption>Verified revenue snapshots</caption>
                  <thead><tr><th scope="col">Run</th><th scope="col">Platform</th><th scope="col">Date</th><th scope="col">Views</th><th scope="col">Revenue</th><th scope="col">Likes</th><th scope="col">Comments</th><th scope="col">Shares</th><th scope="col">Fetched</th></tr></thead>
                  <tbody>{page.items.map((item) => (
                    <tr key={item.id} data-revenue-id={item.id}>
                      <th scope="row"><Link to={`/jobs/${item.job_id}`}>{item.job_id}</Link></th>
                      <td>{item.platform}</td>
                      <td>{item.date}</td>
                      <td>{Number(item.views ?? 0).toLocaleString()}</td>
                      <td>${Number(item.revenue_usd ?? 0).toFixed(2)}</td>
                      <td>{Number(item.likes ?? 0).toLocaleString()}</td>
                      <td>{Number(item.comments ?? 0).toLocaleString()}</td>
                      <td>{Number(item.shares ?? 0).toLocaleString()}</td>
                      <td><time dateTime={item.fetched_at}>{item.fetched_at}</time></td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            </>
          )
        }}
      </ResourceState>
    </section>
  )
}
