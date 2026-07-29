import { useCallback } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../../api'
import { useApp } from '../../context/AppContext'
import { usePollingResource } from '../../hooks/usePollingResource'

// A count and a route, so this reads as a chip in the topbar. The qualifier is
// the only element carrying role="status" — the count chip must not announce
// itself on every poll.
function Banner({ count, qualifier = '' }) {
  const note = qualifier ? <span role="status" className="status-chip status-chip--warning">{qualifier}</span> : null
  if (!(count > 0)) return note
  return (
    <>
      {note}
      <Link
        to="/alerts"
        aria-label={`View ${count} runs needing attention`}
        className="status-chip status-chip--alert"
      >
        <span className="status-chip__mark" aria-hidden="true" />
        <strong>{count} need attention</strong>
      </Link>
    </>
  )
}

export default function AlertBanner({ count, client = api, pollingOptions = {} }) {
  const { operatorToken } = useApp()
  const shouldLoad = count == null && Boolean(operatorToken)
  const load = useCallback(
    (signal) => client.getAlerts(1, { token: operatorToken, signal }),
    [client, operatorToken],
  )
  const resource = usePollingResource(load, {
    enabled: shouldLoad,
    intervalMs: 10_000,
    staleAfterMs: 30_000,
    dependencies: [operatorToken],
    ...pollingOptions,
  })

  if (count != null) return <Banner count={count} />
  if (!operatorToken) return null
  if (resource.status === 'loading' && !resource.hasData) {
    return <div role="status" className="status-chip status-chip--checking">Checking operator alerts…</div>
  }
  if ((resource.status === 'error' || resource.status === 'disconnected') && !resource.hasData) {
    return (
      <span role="alert" className="status-chip status-chip--error">
        <span>{resource.status === 'disconnected' ? 'Alert summary disconnected.' : 'Alert summary unavailable.'}</span>
        <button type="button" className="button button--quiet button--sm" onClick={resource.refresh}>Retry alert summary</button>
      </span>
    )
  }
  const total = Number(resource.data?.total ?? 0)
  if (resource.status === 'stale') return <Banner count={total} qualifier="Alert summary is stale. Showing cached total." />
  if (resource.status === 'disconnected') return <Banner count={total} qualifier="Alert summary disconnected. Showing cached total." />
  if (resource.status === 'error') return <Banner count={total} qualifier="Alert summary unavailable. Showing cached total." />
  return <Banner count={total} />
}
