import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'

import { api } from '../api'
import { usePollingResource } from '../hooks/usePollingResource'

const TOKEN_KEY = 'slur-meter.operator-token'
const AppContext = createContext(null)

function readToken() {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY) || ''
  } catch {
    return ''
  }
}

export function AppProvider({
  children,
  healthLoader,
  healthPolling = {},
  loadHealth = true,
  maxToasts = 5,
  toastDurationMs = 5_000,
}) {
  const [operatorToken, updateOperatorToken] = useState(readToken)
  const [toasts, setToasts] = useState([])
  const nextToastId = useRef(1)
  const toastTimers = useRef(new Map())
  const defaultHealthLoader = useCallback((signal) => api.health({ signal }), [])
  const loadHealthResource = healthLoader || defaultHealthLoader
  const health = usePollingResource(loadHealthResource, {
    intervalMs: 10_000,
    staleAfterMs: 30_000,
    ...healthPolling,
    enabled: loadHealth,
  })

  useEffect(() => {
    try {
      if (operatorToken) window.sessionStorage.setItem(TOKEN_KEY, operatorToken)
      else window.sessionStorage.removeItem(TOKEN_KEY)
    } catch {
      // Session storage is optional; keep the in-memory session usable.
    }
  }, [operatorToken])

  const setOperatorToken = useCallback((value) => {
    updateOperatorToken(typeof value === 'string' ? value.trim() : '')
  }, [])
  const clearOperatorToken = useCallback(() => updateOperatorToken(''), [])

  const dismissToast = useCallback((id) => {
    const timer = toastTimers.current.get(id)
    if (timer != null) clearTimeout(timer)
    toastTimers.current.delete(id)
    setToasts((current) => current.filter((toast) => toast.id !== id))
  }, [])

  const addToast = useCallback((toast) => {
    const id = `toast_${nextToastId.current++}`
    const item = {
      id,
      type: toast?.type === 'error' ? 'error' : (toast?.type || 'info'),
      message: String(toast?.message || 'Notification'),
    }
    setToasts((current) => {
      const next = [...current, item]
      const removed = next.length > maxToasts ? next.slice(0, next.length - maxToasts) : []
      removed.forEach(({ id: removedId }) => {
        const timer = toastTimers.current.get(removedId)
        if (timer != null) clearTimeout(timer)
        toastTimers.current.delete(removedId)
      })
      return next.slice(-maxToasts)
    })
    if (toastDurationMs > 0) {
      const timer = setTimeout(() => dismissToast(id), toastDurationMs)
      toastTimers.current.set(id, timer)
    }
    return id
  }, [dismissToast, maxToasts, toastDurationMs])

  useEffect(() => () => {
    toastTimers.current.forEach((timer) => clearTimeout(timer))
    toastTimers.current.clear()
  }, [])

  const connectivity = useMemo(() => {
    const connected = health.status === 'success' && health.data?.status === 'ok'
    const dispatcherReady = connected && health.data?.dispatcher_ready === true
    let status = 'checking'
    if (health.status === 'disconnected' || health.status === 'stale') status = 'disconnected'
    else if (health.status === 'error') status = 'error'
    else if (connected && dispatcherReady) status = 'ready'
    else if (connected) status = 'dispatcher_unavailable'
    return { connected, dispatcherReady, status }
  }, [health.data, health.status])

  const value = useMemo(() => ({
    operatorToken,
    setOperatorToken,
    clearOperatorToken,
    health,
    connectivity,
    toasts,
    addToast,
    dismissToast,
  }), [addToast, clearOperatorToken, connectivity, dismissToast, health, operatorToken, setOperatorToken, toasts])

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>
}

export function useApp() {
  const value = useContext(AppContext)
  if (!value) throw new Error('useApp must be used within AppProvider')
  return value
}

export { TOKEN_KEY }
