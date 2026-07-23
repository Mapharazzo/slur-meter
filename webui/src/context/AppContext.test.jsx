import { act, renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ApiError } from '../api'
import { AppProvider, useApp } from './AppContext'

const wrapper = (props = {}) => function Wrapper({ children }) {
  return <AppProvider {...props}>{children}</AppProvider>
}

describe('AppProvider', () => {
  it('initializes and persists operator tokens in session storage only', async () => {
    sessionStorage.setItem('slur-meter.operator-token', 'session-token')
    localStorage.setItem('slur-meter.operator-token', 'local-token')
    const { result } = renderHook(() => useApp(), { wrapper: wrapper({ loadHealth: false }) })
    expect(result.current.operatorToken).toBe('session-token')

    act(() => result.current.setOperatorToken('new-token'))
    expect(sessionStorage.getItem('slur-meter.operator-token')).toBe('new-token')
    expect(localStorage.getItem('slur-meter.operator-token')).toBe('local-token')
    act(() => result.current.clearOperatorToken())
    expect(sessionStorage.getItem('slur-meter.operator-token')).toBeNull()
  })

  it('survives unavailable session storage without logging or rendering the token', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new DOMException('denied') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('denied') })
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { result } = renderHook(() => useApp(), { wrapper: wrapper({ loadHealth: false }) })

    act(() => result.current.setOperatorToken('never-show-this'))

    expect(result.current.operatorToken).toBe('never-show-this')
    expect(result.current.toasts).toEqual([])
    expect(consoleSpy).not.toHaveBeenCalled()
    expect(document.body).not.toHaveTextContent('never-show-this')
  })

  it('derives connectivity and dispatcher readiness from injected health polling', async () => {
    const health = vi.fn().mockResolvedValue({ status: 'ok', dispatcher_ready: true })
    const { result } = renderHook(() => useApp(), {
      wrapper: wrapper({ healthLoader: health, healthPolling: { intervalMs: 10_000 } }),
    })
    await act(async () => {})

    expect(health).toHaveBeenCalledWith(expect.any(AbortSignal))
    expect(result.current.health.data).toEqual({ status: 'ok', dispatcher_ready: true })
    expect(result.current.connectivity).toMatchObject({ connected: true, dispatcherReady: true, status: 'ready' })
  })

  it.each([
    [{ status: 'ok', dispatcher_ready: false }, null, { connected: true, dispatcherReady: false, status: 'dispatcher_unavailable' }],
    [null, new ApiError('Offline', { code: 'network_error', retryable: true }), { connected: false, dispatcherReady: false, status: 'disconnected' }],
    [null, new ApiError('Bad health', { code: 'bad_health', status: 500 }), { connected: false, dispatcherReady: false, status: 'error' }],
  ])('maps health result %j and error %j truthfully', async (value, error, expected) => {
    const health = error ? vi.fn().mockRejectedValue(error) : vi.fn().mockResolvedValue(value)
    const { result } = renderHook(() => useApp(), {
      wrapper: wrapper({ healthLoader: health, healthPolling: { intervalMs: 10_000 } }),
    })
    await act(async () => {})
    expect(result.current.connectivity).toEqual(expected)
  })

  it('bounds toasts, generates stable IDs, dismisses, and clears expiry timers', () => {
    vi.useFakeTimers()
    const clear = vi.spyOn(globalThis, 'clearTimeout')
    const { result, unmount } = renderHook(() => useApp(), {
      wrapper: wrapper({ loadHealth: false, maxToasts: 2, toastDurationMs: 500 }),
    })
    let first
    act(() => {
      first = result.current.addToast({ message: 'one' })
      result.current.addToast({ message: 'two' })
      result.current.addToast({ message: 'three' })
    })
    expect(result.current.toasts.map((toast) => toast.message)).toEqual(['two', 'three'])
    expect(new Set(result.current.toasts.map((toast) => toast.id)).size).toBe(2)
    act(() => result.current.dismissToast(result.current.toasts[0].id))
    expect(result.current.toasts).toHaveLength(1)
    expect(first).toMatch(/^toast_/)
    act(() => vi.advanceTimersByTime(500))
    expect(result.current.toasts).toHaveLength(0)
    unmount()
    expect(clear).toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
    vi.useRealTimers()
  })
})
