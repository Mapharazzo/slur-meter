function defaultIsEmpty(data) {
  return data == null || (Array.isArray(data) && data.length === 0)
}

function Warning({ children }) {
  return <div role="alert" className="mb-3 rounded-lg border border-amber-400/30 bg-amber-500/10 p-3 text-sm text-amber-200">{children}</div>
}

function RetryButton({ onRetry }) {
  if (!onRetry) return null
  return <button type="button" aria-label="Retry resource" onClick={onRetry} className="mt-3 rounded-lg bg-white/10 px-3 py-2 text-sm">Retry</button>
}

export default function ResourceState({
  resource,
  children,
  isEmpty = defaultIsEmpty,
  emptyMessage = 'No results found.',
  loadingMessage = 'Loading…',
  onRetry,
}) {
  const { status, data, error, hasData = data !== undefined } = resource || {}
  const content = () => (typeof children === 'function' ? children(data) : children)
  const retry = onRetry ?? resource?.refresh

  if (status === 'loading' && !hasData) {
    return <div role="status" aria-live="polite">{loadingMessage}</div>
  }
  if (status === 'success') {
    if (isEmpty(data)) return <div>{emptyMessage}</div>
    return content()
  }
  if (status === 'stale' && hasData) {
    return <><Warning>Showing stale cached data. Refresh to check for updates.</Warning>{content()}<RetryButton onRetry={retry} /></>
  }
  if (status === 'disconnected' && hasData) {
    return <><Warning>Connection unavailable. Showing the last received data.</Warning>{content()}<RetryButton onRetry={retry} /></>
  }
  if (status === 'error' && hasData) {
    return <><Warning>{error?.message || 'The resource could not be loaded.'} Showing the last received data.</Warning>{content()}<RetryButton onRetry={retry} /></>
  }
  if (status === 'error' || status === 'disconnected') {
    return <div role="alert"><p>{error?.message || (status === 'disconnected' ? 'Connection unavailable.' : 'The resource could not be loaded.')}</p><RetryButton onRetry={retry} /></div>
  }
  return null
}
