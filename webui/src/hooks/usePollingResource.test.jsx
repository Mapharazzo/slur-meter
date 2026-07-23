import { StrictMode } from 'react'
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../api'
import { usePollingResource } from './usePollingResource'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('usePollingResource', () => {
  it('does not report success before an initially disabled resource has loaded', async () => {
    const load = vi.fn()
    const { result } = renderHook(() => usePollingResource(load, { enabled: false }))
    await act(async () => {})

    expect(result.current).toMatchObject({
      status: 'loading',
      hasData: false,
      isLoading: true,
      isStopped: true,
    })
    expect(load).not.toHaveBeenCalled()
  })

  it('distinguishes initial loading from a successful empty result', async () => {
    const pending = deferred()
    const load = vi.fn(() => pending.promise)
    const { result } = renderHook(() => usePollingResource(load, { intervalMs: 1_000 }))
    await act(async () => {})
    expect(result.current).toMatchObject({ status: 'loading', data: undefined, isLoading: true, hasData: false })

    await act(async () => pending.resolve([]))

    expect(result.current).toMatchObject({ status: 'success', data: [], isLoading: false, hasData: true })
    expect(result.current.lastSuccessAt).toEqual(expect.any(Number))
  })

  it('uses recursive scheduling after settle and never overlaps loads', async () => {
    const first = deferred()
    const second = deferred()
    const load = vi.fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
      .mockResolvedValue('third')
    renderHook(() => usePollingResource(load, { intervalMs: 100 }))
    await act(async () => {})

    expect(load).toHaveBeenCalledTimes(1)
    await act(async () => vi.advanceTimersByTimeAsync(500))
    expect(load).toHaveBeenCalledTimes(1)
    await act(async () => first.resolve('first'))
    await act(async () => vi.advanceTimersByTimeAsync(99))
    expect(load).toHaveBeenCalledTimes(1)
    await act(async () => vi.advanceTimersByTimeAsync(1))
    expect(load).toHaveBeenCalledTimes(2)
    await act(async () => vi.advanceTimersByTimeAsync(500))
    expect(load).toHaveBeenCalledTimes(2)
    await act(async () => second.resolve('second'))
  })

  it('aborts on unmount and ignores promises that settle afterward', async () => {
    const pending = deferred()
    let signal
    const load = vi.fn((value) => { signal = value; return pending.promise })
    const { result, unmount } = renderHook(() => usePollingResource(load))
    await act(async () => {})

    unmount()
    expect(signal.aborted).toBe(true)
    await act(async () => pending.resolve('late'))
    expect(result.current.data).toBeUndefined()
  })

  it('manual refresh supersedes and generation-fences an abort-ignoring request', async () => {
    const oldRequest = deferred()
    const newRequest = deferred()
    const signals = []
    const load = vi.fn((signal) => {
      signals.push(signal)
      return signals.length === 1 ? oldRequest.promise : newRequest.promise
    })
    const { result } = renderHook(() => usePollingResource(load))
    await act(async () => {})

    await act(async () => result.current.refresh())
    expect(signals[0].aborted).toBe(true)
    expect(load).toHaveBeenCalledTimes(2)
    await act(async () => newRequest.resolve('new'))
    await act(async () => oldRequest.resolve('old'))
    expect(result.current.data).toBe('new')
  })

  it('retains last good data while classifying transport and application failures', async () => {
    const load = vi.fn()
      .mockResolvedValueOnce({ id: 1 })
      .mockRejectedValueOnce(new ApiError('Offline', { code: 'network_error', retryable: true }))
      .mockRejectedValueOnce(new ApiError('Invalid state', { code: 'conflict', status: 409 }))
    const { result } = renderHook(() => usePollingResource(load, { intervalMs: 10, initialBackoffMs: 20 }))
    await act(async () => {})
    expect(result.current.data).toEqual({ id: 1 })

    act(() => result.current.refresh())
    await act(async () => {})
    expect(result.current).toMatchObject({ status: 'disconnected', data: { id: 1 }, isDisconnected: true })

    act(() => result.current.refresh())
    await act(async () => {})
    expect(result.current).toMatchObject({ status: 'error', data: { id: 1 }, isDisconnected: false })
  })

  it('uses bounded exponential backoff and resets it after success', async () => {
    const load = vi.fn()
      .mockRejectedValueOnce(new TypeError('offline'))
      .mockRejectedValueOnce(new TypeError('offline'))
      .mockResolvedValueOnce('ok')
      .mockRejectedValueOnce(new TypeError('offline'))
      .mockResolvedValue('again')
    renderHook(() => usePollingResource(load, {
      intervalMs: 50,
      initialBackoffMs: 100,
      maxBackoffMs: 200,
    }))
    await act(async () => {})
    await act(async () => vi.advanceTimersByTimeAsync(99))
    expect(load).toHaveBeenCalledTimes(1)
    await act(async () => vi.advanceTimersByTimeAsync(1))
    expect(load).toHaveBeenCalledTimes(2)
    await act(async () => vi.advanceTimersByTimeAsync(199))
    expect(load).toHaveBeenCalledTimes(2)
    await act(async () => vi.advanceTimersByTimeAsync(1))
    expect(load).toHaveBeenCalledTimes(3)
    await act(async () => vi.advanceTimersByTimeAsync(50))
    expect(load).toHaveBeenCalledTimes(4)
    await act(async () => vi.advanceTimersByTimeAsync(100))
    expect(load).toHaveBeenCalledTimes(5)
  })

  it('becomes visibly stale at the threshold and recovers on success', async () => {
    const second = deferred()
    const load = vi.fn().mockResolvedValueOnce('cached').mockReturnValueOnce(second.promise)
    const { result } = renderHook(() => usePollingResource(load, { intervalMs: 10_000, staleAfterMs: 200 }))
    await act(async () => {})
    expect(result.current.status).toBe('success')

    await act(async () => vi.advanceTimersByTimeAsync(200))
    expect(result.current).toMatchObject({ status: 'stale', isStale: true, data: 'cached' })
    act(() => result.current.refresh())
    await act(async () => second.resolve('fresh'))
    expect(result.current).toMatchObject({ status: 'success', isStale: false, data: 'fresh' })
  })

  it('pauses while hidden and refreshes immediately when visible without overlap', async () => {
    const first = deferred()
    const second = deferred()
    const load = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    const remove = vi.spyOn(document, 'removeEventListener')
    const { unmount } = renderHook(() => usePollingResource(load, { intervalMs: 100, pauseWhenHidden: true }))
    await act(async () => {})
    visibility.mockReturnValue('hidden')
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    await act(async () => first.resolve('first'))
    await act(async () => vi.advanceTimersByTimeAsync(500))
    expect(load).toHaveBeenCalledTimes(1)

    visibility.mockReturnValue('visible')
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    expect(load).toHaveBeenCalledTimes(2)
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    expect(load).toHaveBeenCalledTimes(2)
    await act(async () => second.resolve('second'))
    unmount()
    expect(remove).toHaveBeenCalledWith('visibilitychange', expect.any(Function))
    expect(vi.getTimerCount()).toBe(0)
  })

  it('stops automatic polling after a terminal success while preserving manual refresh', async () => {
    const load = vi.fn().mockResolvedValueOnce({ state: 'completed' }).mockResolvedValue({ state: 'completed', fresh: true })
    const { result } = renderHook(() => usePollingResource(load, {
      intervalMs: 100,
      staleAfterMs: 50,
      isTerminal: (data) => data.state === 'completed',
    }))
    await act(async () => {})
    expect(result.current.isStopped).toBe(true)
    await act(async () => vi.advanceTimersByTimeAsync(1_000))
    expect(load).toHaveBeenCalledTimes(1)
    expect(result.current).toMatchObject({ status: 'stale', isStale: true, isStopped: true })
    act(() => result.current.refresh())
    await act(async () => {})
    expect(load).toHaveBeenCalledTimes(2)
  })

  it('stop aborts active work and prevents automatic refresh', async () => {
    const pending = deferred()
    let signal
    const load = vi.fn((currentSignal) => { signal = currentSignal; return pending.promise })
    const { result } = renderHook(() => usePollingResource(load, { intervalMs: 10 }))
    await act(async () => {})
    await act(async () => result.current.stop())
    expect(signal.aborted).toBe(true)
    expect(result.current.isStopped).toBe(true)
    await act(async () => pending.resolve('ignored'))
    await act(async () => vi.advanceTimersByTimeAsync(100))
    expect(load).toHaveBeenCalledTimes(1)
  })

  it('does not duplicate the initial load under React StrictMode', async () => {
    const pending = deferred()
    let signal
    const load = vi.fn((currentSignal) => { signal = currentSignal; return pending.promise })
    const strict = ({ children }) => <StrictMode>{children}</StrictMode>
    const { unmount } = renderHook(() => usePollingResource((currentSignal) => load(currentSignal)), { wrapper: strict })

    await act(async () => {})

    expect(load).toHaveBeenCalledTimes(1)
    unmount()
    expect(signal.aborted).toBe(true)
  })

  it('replaces the polling generation only when an explicit dependency changes', async () => {
    const second = deferred()
    const load = vi.fn().mockResolvedValueOnce('job-one').mockReturnValueOnce(second.promise)
    const strict = ({ children }) => <StrictMode>{children}</StrictMode>
    const { result, rerender } = renderHook(
      ({ id }) => usePollingResource((signal) => load(id, signal), { dependencies: [id], intervalMs: 10_000 }),
      { initialProps: { id: 'one' }, wrapper: strict },
    )
    await act(async () => {})
    expect(result.current.data).toBe('job-one')

    rerender({ id: 'one' })
    await act(async () => {})
    expect(load).toHaveBeenCalledTimes(1)

    rerender({ id: 'two' })
    await act(async () => {})
    expect(load).toHaveBeenCalledTimes(2)
    expect(result.current).toMatchObject({ status: 'loading', data: undefined, hasData: false })
    await act(async () => second.resolve('job-two'))
    expect(result.current.data).toBe('job-two')
  })

  it('aborts and reports stopped when enabled changes to false', async () => {
    const pending = deferred()
    let signal
    const load = vi.fn((currentSignal) => { signal = currentSignal; return pending.promise })
    const { result, rerender } = renderHook(
      ({ enabled }) => usePollingResource(load, { enabled }),
      { initialProps: { enabled: true } },
    )
    await act(async () => {})
    rerender({ enabled: false })
    await act(async () => {})

    expect(signal.aborted).toBe(true)
    expect(result.current.isStopped).toBe(true)
  })

  it('keeps the original stale deadline when disabled after success', async () => {
    const load = vi.fn().mockResolvedValue('cached')
    const { result, rerender } = renderHook(
      ({ enabled }) => usePollingResource(load, { enabled, intervalMs: 1_000, staleAfterMs: 100 }),
      { initialProps: { enabled: true } },
    )
    await act(async () => {})
    await act(async () => vi.advanceTimersByTimeAsync(40))
    rerender({ enabled: false })
    await act(async () => {})
    await act(async () => vi.advanceTimersByTimeAsync(59))
    expect(result.current).toMatchObject({ status: 'success', isStale: false, isStopped: true })

    await act(async () => vi.advanceTimersByTimeAsync(1))

    expect(result.current).toMatchObject({ status: 'stale', isStale: true, isStopped: true, data: 'cached' })
    expect(load).toHaveBeenCalledTimes(1)
  })

  it('does not run an extra queued refresh after manual supersession', async () => {
    const first = deferred()
    const second = deferred()
    const load = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    const { result } = renderHook(() => usePollingResource(load))
    await act(async () => {})

    visibility.mockReturnValue('hidden')
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    visibility.mockReturnValue('visible')
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    await act(async () => result.current.refresh())
    await act(async () => second.resolve('fresh'))

    expect(load).toHaveBeenCalledTimes(2)
  })
})
