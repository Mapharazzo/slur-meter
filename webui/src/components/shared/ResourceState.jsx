function defaultIsEmpty(data) {
  return data == null || (Array.isArray(data) && data.length === 0)
}

function RetryButton({ onRetry, disabled = false }) {
  if (!onRetry) return null
  return (
    <button
      type="button"
      aria-label="Retry resource"
      disabled={disabled}
      onClick={onRetry}
      className="button button--sm"
    >
      Retry
    </button>
  )
}

/**
 * Every degraded state keeps the last good data on screen and says why it may
 * be out of date, rather than replacing the page with an error.
 *
 * The three slots below are always rendered in the same positions under the
 * same root, even when a slot is empty. Returning a different shape per status
 * made React remount the whole subtree on any transition, which reset live
 * children — a playing video would drop its blob and restart from zero the
 * moment the resource aged.
 */
export default function ResourceState({
  resource,
  children,
  isEmpty = defaultIsEmpty,
  emptyMessage = 'No results found.',
  loadingMessage = 'Loading…',
  onRetry,
  retryDisabled = false,
}) {
  const { status, data, error, hasData = data !== undefined } = resource || {}
  const content = () => (typeof children === 'function' ? children(data) : children)
  const retry = onRetry ?? resource?.refresh

  let notice = null
  let body = null
  let showRetry = false

  const warning = (message) => <div role="alert" className="notice notice--warning">{message}</div>

  if (status === 'loading' && !hasData) {
    body = <div role="status" aria-live="polite" className="state-block">{loadingMessage}</div>
  } else if (status === 'success') {
    body = isEmpty(data) ? <div className="state-block">{emptyMessage}</div> : content()
  } else if (hasData && (status === 'stale' || status === 'disconnected' || status === 'error')) {
    if (status === 'stale') notice = warning('Showing stale cached data. Refresh to check for updates.')
    else if (status === 'disconnected') notice = warning('Connection unavailable. Showing the last received data.')
    else notice = warning(`${error?.message || 'The resource could not be loaded.'} Showing the last received data.`)
    body = content()
    showRetry = true
  } else if (status === 'error' || status === 'disconnected') {
    notice = (
      <div role="alert" className="notice notice--danger">
        <p>{error?.message || (status === 'disconnected' ? 'Connection unavailable.' : 'The resource could not be loaded.')}</p>
      </div>
    )
    showRetry = true
  } else {
    return null
  }

  return (
    <div className="resource-state">
      {notice}
      {body}
      {showRetry ? <RetryButton onRetry={retry} disabled={retryDisabled} /> : null}
    </div>
  )
}
