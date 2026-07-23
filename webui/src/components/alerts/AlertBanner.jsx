import { useCallback } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../../api'
import { useApp } from '../../context/AppContext'
import { usePollingResource } from '../../hooks/usePollingResource'

function Banner({ count, qualifier = '' }) {
  if (!(count > 0)) {
    return qualifier ? (
      <aside aria-label="Operator attention summary">
        <p role="status">{qualifier}</p>
        <p>No cached runs need attention.</p>
      </aside>
    ) : null
  }
  return (
    <aside aria-label="Operator attention summary">
      {qualifier && <p role="status">{qualifier}</p>}
      <Link
        to="/alerts"
        aria-label={`View ${count} runs needing attention`}
        className="block bg-red-500/10 border-b border-red-500/20 px-6 py-2.5"
      >
        <strong>{count} need attention</strong>
        <span> · View alerts</span>
      </Link>
    </aside>
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
    return <div role="status">Checking operator alerts…</div>
  }
  if ((resource.status === 'error' || resource.status === 'disconnected') && !resource.hasData) {
    return (
      <aside role="alert">
        <span>{resource.status === 'disconnected' ? 'Alert summary disconnected.' : 'Alert summary unavailable.'}</span>
        <button type="button" onClick={resource.refresh}>Retry alert summary</button>
      </aside>
    )
  }
  const total = Number(resource.data?.total ?? 0)
  if (resource.status === 'stale') return <Banner count={total} qualifier="Alert summary is stale. Showing cached total." />
  if (resource.status === 'disconnected') return <Banner count={total} qualifier="Alert summary disconnected. Showing cached total." />
  if (resource.status === 'error') return <Banner count={total} qualifier="Alert summary unavailable. Showing cached total." />
  return <Banner count={total} />
}
