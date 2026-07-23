import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '../api'

function disconnectedError(error) {
  if (error instanceof ApiError) {
    return ['network_error', 'timeout'].includes(error.code)
  }
  return error instanceof TypeError
}

function safePollingError(error) {
  if (error instanceof ApiError) return error
  if (error instanceof TypeError) {
    return new ApiError('The server could not be reached.', {
      code: 'network_error',
      retryable: true,
    })
  }
  return new ApiError('The resource could not be loaded.', {
    code: 'polling_error',
    retryable: false,
  })
}

/**
 * Poll an abort-aware resource without overlapping requests.
 *
 * Returns `{ data, error, status, isLoading, isSuccess, isDisconnected,
 * isStale, isStopped, hasData, lastAttemptAt, lastSuccessAt, refresh, stop }`.
 * `refresh()` always performs one superseding request, even after terminal or
 * explicit stop; it does not restart automatic polling when the result is
 * terminal or `stop()` remains in force.
 */
export function usePollingResource(load, options = {}) {
  const {
    enabled = true,
    intervalMs = 5_000,
    initialBackoffMs = intervalMs,
    maxBackoffMs = 30_000,
    staleAfterMs = 15_000,
    pauseWhenHidden = true,
    isTerminal,
    initialData,
    dependencies = [],
  } = options
  const initialHasData = initialData !== undefined
  const [state, setState] = useState(() => ({
    data: initialData,
    error: null,
    status: initialHasData ? 'success' : 'loading',
    hasData: initialHasData,
    isStale: false,
    isStopped: !enabled,
    lastAttemptAt: null,
    lastSuccessAt: initialHasData ? Date.now() : null,
  }))
  const lastSuccessAtRef = useRef(state.lastSuccessAt)
  const mountedRef = useRef(false)
  const timerRef = useRef(null)
  const staleTimerRef = useRef(null)
  const controllerRef = useRef(null)
  const requestGenerationRef = useRef(0)
  const effectGenerationRef = useRef(0)
  const failuresRef = useRef(0)
  const explicitStopRef = useRef(!enabled)
  const terminalStopRef = useRef(false)
  const hiddenRef = useRef(false)
  const refreshQueuedRef = useRef(false)
  const runRef = useRef(null)
  const isTerminalRef = useRef(isTerminal)
  const loadRef = useRef(load)
  const previousDependenciesRef = useRef(dependencies)
  const dependencyChanged = (
    previousDependenciesRef.current.length !== dependencies.length
    || dependencies.some((value, index) => !Object.is(value, previousDependenciesRef.current[index]))
  )
  isTerminalRef.current = isTerminal
  loadRef.current = load

  const clearSchedule = useCallback(() => {
    if (timerRef.current != null) clearTimeout(timerRef.current)
    timerRef.current = null
  }, [])

  const clearStaleSchedule = useCallback(() => {
    if (staleTimerRef.current != null) clearTimeout(staleTimerRef.current)
    staleTimerRef.current = null
  }, [])

  const scheduleStale = useCallback((successAt) => {
    clearStaleSchedule()
    if (!(staleAfterMs >= 0) || !Number.isFinite(staleAfterMs)) return
    const elapsed = Math.max(0, Date.now() - successAt)
    const remaining = Math.max(0, staleAfterMs - elapsed)
    staleTimerRef.current = setTimeout(() => {
      if (!mountedRef.current) return
      setState((current) => {
        if (current.lastSuccessAt !== successAt || !current.hasData) return current
        return {
          ...current,
          isStale: true,
          status: current.status === 'success' ? 'stale' : current.status,
        }
      })
    }, remaining)
  }, [clearStaleSchedule, staleAfterMs])

  const schedule = useCallback((delay) => {
    clearSchedule()
    if (
      !mountedRef.current
      || explicitStopRef.current
      || terminalStopRef.current
      || (pauseWhenHidden && hiddenRef.current)
    ) return
    timerRef.current = setTimeout(() => runRef.current?.({ supersede: false }), delay)
  }, [clearSchedule, pauseWhenHidden])

  const run = useCallback(async ({ supersede = true } = {}) => {
    if (!mountedRef.current) return
    clearSchedule()
    if (supersede) refreshQueuedRef.current = false
    if (controllerRef.current) {
      if (!supersede) {
        refreshQueuedRef.current = true
        return
      }
      controllerRef.current.abort()
    }

    const controller = new AbortController()
    controllerRef.current = controller
    const generation = ++requestGenerationRef.current
    setState((current) => ({
      ...current,
      error: null,
      status: current.hasData ? current.status : 'loading',
      lastAttemptAt: Date.now(),
    }))

    try {
      const data = await loadRef.current(controller.signal)
      if (!mountedRef.current || generation !== requestGenerationRef.current) return
      failuresRef.current = 0
      const successAt = Date.now()
      lastSuccessAtRef.current = successAt
      const terminal = Boolean(isTerminalRef.current?.(data))
      terminalStopRef.current = terminal
      setState((current) => ({
        ...current,
        data,
        error: null,
        status: 'success',
        hasData: true,
        isStale: false,
        isStopped: explicitStopRef.current || terminal,
        lastSuccessAt: successAt,
      }))
      scheduleStale(successAt)
    } catch (error) {
      if (!mountedRef.current || generation !== requestGenerationRef.current) return
      if (controller.signal.aborted) return
      failuresRef.current += 1
      const safeError = safePollingError(error)
      setState((current) => ({
        ...current,
        error: safeError,
        status: disconnectedError(error) ? 'disconnected' : 'error',
        isStopped: explicitStopRef.current || terminalStopRef.current,
      }))
    } finally {
      if (!mountedRef.current || generation !== requestGenerationRef.current) return
      controllerRef.current = null
      if (refreshQueuedRef.current) {
        refreshQueuedRef.current = false
        runRef.current?.({ supersede: false })
        return
      }
      if (failuresRef.current > 0) {
        const delay = Math.min(
          maxBackoffMs,
          initialBackoffMs * (2 ** (failuresRef.current - 1)),
        )
        schedule(delay)
      } else {
        schedule(intervalMs)
      }
    }
  }, [clearSchedule, clearStaleSchedule, initialBackoffMs, intervalMs, maxBackoffMs, schedule, scheduleStale])

  runRef.current = run

  const refresh = useCallback(() => {
    terminalStopRef.current = false
    runRef.current?.({ supersede: true })
  }, [])

  const stop = useCallback(() => {
    explicitStopRef.current = true
    refreshQueuedRef.current = false
    clearSchedule()
    controllerRef.current?.abort()
    controllerRef.current = null
    requestGenerationRef.current += 1
    setState((current) => ({ ...current, isStopped: true }))
  }, [clearSchedule])

  useEffect(() => {
    previousDependenciesRef.current = dependencies
    mountedRef.current = true
    explicitStopRef.current = !enabled
    terminalStopRef.current = false
    failuresRef.current = 0
    refreshQueuedRef.current = false
    if (dependencyChanged) {
      lastSuccessAtRef.current = null
      setState({
        data: undefined,
        error: null,
        status: 'loading',
        hasData: false,
        isStale: false,
        isStopped: !enabled,
        lastAttemptAt: null,
        lastSuccessAt: null,
      })
    } else {
      setState((current) => (
        current.isStopped === !enabled ? current : { ...current, isStopped: !enabled }
      ))
      if (lastSuccessAtRef.current != null) scheduleStale(lastSuccessAtRef.current)
    }
    hiddenRef.current = pauseWhenHidden && document.visibilityState === 'hidden'
    const effectGeneration = ++effectGenerationRef.current
    if (enabled && !hiddenRef.current) {
      queueMicrotask(() => {
        if (mountedRef.current && effectGeneration === effectGenerationRef.current) {
          runRef.current?.({ supersede: true })
        }
      })
    }

    const onVisibilityChange = () => {
      hiddenRef.current = document.visibilityState === 'hidden'
      if (!pauseWhenHidden) return
      if (hiddenRef.current) {
        clearSchedule()
      } else if (!explicitStopRef.current && !terminalStopRef.current) {
        runRef.current?.({ supersede: false })
      }
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      mountedRef.current = false
      effectGenerationRef.current += 1
      clearSchedule()
      clearStaleSchedule()
      controllerRef.current?.abort()
      controllerRef.current = null
      refreshQueuedRef.current = false
      requestGenerationRef.current += 1
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [clearSchedule, clearStaleSchedule, enabled, pauseWhenHidden, run, scheduleStale, ...dependencies])

  return {
    ...state,
    isLoading: state.status === 'loading',
    isSuccess: state.status === 'success',
    isDisconnected: state.status === 'disconnected',
    refresh,
    stop,
  }
}

export default usePollingResource
