import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import axe from 'axe-core'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../../api'
import { AppProvider } from '../../context/AppContext'
import AlertBanner from '../alerts/AlertBanner'
import AlertList from '../alerts/AlertList'
import CostBreakdown from '../costs/CostBreakdown'
import CostDashboard from '../costs/CostDashboard'
import Leaderboard from '../leaderboard/Leaderboard'
import RevenueDashboard from '../revenue/RevenueDashboard'

function client(overrides = {}) {
  return {
    getLeaderboard: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    getAggregateCosts: vi.fn().mockResolvedValue([]),
    getRevenue: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    getAlerts: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    ...overrides,
  }
}

function renderRoute(ui, token = 'secondary-token') {
  sessionStorage.setItem('slur-meter.operator-token', token)
  return render(
    <MemoryRouter>
      <AppProvider loadHealth={false}>{ui}</AppProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.useRealTimers()
})

describe('honest secondary operational routes', () => {
  it('renders the completed-videos gallery: one card per movie, metrics, verdict, and explicit auth', async () => {
    const apiClient = client({
      getLeaderboard: vi.fn().mockResolvedValue({
        items: [
          { job_id: 'job_new', source_imdb_id: 'tt0110912', label: 'Pulp Fiction', hard: 0, soft: 2, f_bombs: 3, rating: '😬 EDGY', peak_minute: 12, total_views: 0, finished_at: '2026-07-23T12:00:00Z' },
          // Duplicate movie (same imdb id) from an earlier render — must collapse to one card.
          { job_id: 'job_old', source_imdb_id: 'tt0110912', label: 'Pulp Fiction', hard: 9, soft: 9, f_bombs: 9, rating: '💀 HAZMAT', peak_minute: 3, total_views: 5, finished_at: '2026-07-20T12:00:00Z' },
        ],
        total: 2,
      }),
    })
    const { container } = renderRoute(<Leaderboard client={apiClient} pollingOptions={{ intervalMs: 60_000 }} />)

    expect(await screen.findByRole('heading', { level: 1, name: /completed videos/i })).toBeInTheDocument()
    // Deduped by movie id — one card, linking to the latest render.
    const links = screen.getAllByRole('link', { name: /pulp fiction/i })
    expect(links).toHaveLength(1)
    expect(links[0]).toHaveAttribute('href', '/jobs/job_new')
    expect(screen.getByText('0', { selector: '[data-metric="hard"]' })).toBeInTheDocument()
    expect(screen.getByText('3', { selector: '[data-metric="f-bombs"]' })).toBeInTheDocument()
    expect(screen.getByText('😬 EDGY', { selector: '[data-metric="rating"]' })).toBeInTheDocument()
    expect(screen.getByText(/1 of 1 movie/i)).toBeInTheDocument()
    expect(apiClient.getLeaderboard).toHaveBeenCalledWith({ token: 'secondary-token', signal: expect.any(AbortSignal) })
    expect((await axe.run(container)).violations).toEqual([])
  })

  it('renders real aggregate costs and preserves zeroes in a responsive semantic table', async () => {
    const apiClient = client({
      getAggregateCosts: vi.fn().mockResolvedValue([
        { period: 'all', category: 'api_tmdb', provider: 'tmdb', total_usd: 0, total_units: 0, count: 1 },
      ]),
    })
    renderRoute(<CostDashboard client={apiClient} pollingOptions={{ intervalMs: 60_000 }} />)

    expect(await screen.findByRole('heading', { level: 1, name: /cost dashboard/i })).toBeInTheDocument()
    const table = screen.getByRole('table', { name: /aggregate operational costs/i })
    expect(within(table).getByRole('columnheader', { name: /category/i })).toHaveAttribute('scope', 'col')
    expect(within(table).getByText('$0.0000')).toBeInTheDocument()
    expect(within(table).getAllByText('0').length).toBeGreaterThan(0)
    expect(table.parentElement).toHaveClass('overflow-x-auto')
    expect(apiClient.getAggregateCosts).toHaveBeenCalledWith(
      { group_by: 'category' },
      { token: 'secondary-token', signal: expect.any(AbortSignal) },
    )
  })

  it('renders real revenue rows with stable IDs, totals, zero values, timestamps, and canonical job links', async () => {
    const apiClient = client({
      getRevenue: vi.fn().mockResolvedValue({
        items: [{
          id: 41, job_id: 'job_alpha', platform: 'youtube', date: '2026-07-22',
          views: 0, revenue_usd: 0, likes: 0, comments: 0, shares: 0,
          fetched_at: '2026-07-23T10:00:00Z',
        }],
        total: 1,
      }),
    })
    renderRoute(<RevenueDashboard client={apiClient} pollingOptions={{ intervalMs: 60_000 }} />)

    expect(await screen.findByRole('heading', { level: 1, name: /revenue and views/i })).toBeInTheDocument()
    expect(screen.getAllByText('$0.00')).toHaveLength(2)
    expect(screen.getByText('0 total views')).toBeInTheDocument()
    const row = screen.getByRole('row', { name: /job_alpha.*youtube/i })
    expect(row).toHaveAttribute('data-revenue-id', '41')
    expect(within(row).getByRole('link', { name: 'job_alpha' })).toHaveAttribute('href', '/jobs/job_alpha')
    expect(within(row).getByText('2026-07-23T10:00:00Z').closest('time')).toHaveAttribute('datetime', '2026-07-23T10:00:00Z')
    expect(screen.getByRole('table', { name: /verified revenue snapshots/i }).parentElement).toHaveClass('overflow-x-auto')
    expect(apiClient.getRevenue).toHaveBeenCalledWith(undefined, { token: 'secondary-token', signal: expect.any(AbortSignal) })
  })

  it('uses durable alert identity, canonical links, exact state text, valid timestamps, and an honest banner affordance', async () => {
    const apiClient = client({
      getAlerts: vi.fn().mockResolvedValue({
        items: [
          { job_id: 'job_alpha', state: 'failed', message: 'Encode stopped.', created_at: '2026-07-23T10:00:00Z' },
          { job_id: 'job_beta', state: 'needs_attention', message: 'Choose a subtitle.', created_at: 'not-a-date' },
        ],
        total: 7,
      }),
    })
    const { container } = renderRoute(
      <>
        <AlertBanner count={7} />
        <AlertList client={apiClient} pollingOptions={{ intervalMs: 60_000 }} />
      </>,
    )

    expect(await screen.findByRole('heading', { level: 1, name: /^alerts$/i })).toBeInTheDocument()
    expect(screen.getAllByText('7 need attention')).toHaveLength(2)
    expect(screen.getByRole('link', { name: /view 7 runs needing attention/i })).toHaveAttribute('href', '/alerts')
    expect(screen.getByRole('link', { name: /job_alpha.*failed/i })).toHaveAttribute('href', '/jobs/job_alpha')
    expect(screen.getByText('needs_attention')).toBeInTheDocument()
    expect(screen.getByText('2026-07-23T10:00:00Z').closest('time')).toHaveAttribute('datetime', '2026-07-23T10:00:00Z')
    expect(screen.getByText('Invalid timestamp')).toBeInTheDocument()
    expect(screen.queryByText(/pipeline failure|release failure/i)).not.toBeInTheDocument()
    expect(apiClient.getAlerts).toHaveBeenCalledWith(100, { token: 'secondary-token', signal: expect.any(AbortSignal) })
    expect((await axe.run(container)).violations).toEqual([])
  })

  it('renders compact honest alert-summary loading, disconnected, and stale cached states', async () => {
    const pendingView = renderRoute(
      <AlertBanner client={client({ getAlerts: vi.fn(() => new Promise(() => {})) })} />,
    )
    expect(screen.getByRole('status')).toHaveTextContent(/checking operator alerts/i)
    pendingView.unmount()

    const disconnected = client({
      getAlerts: vi.fn().mockRejectedValue(new ApiError('Offline', { code: 'network_error', retryable: true })),
    })
    const disconnectedView = renderRoute(<AlertBanner client={disconnected} />)
    expect(await screen.findByRole('alert')).toHaveTextContent(/alert summary disconnected/i)
    expect(screen.getByRole('button', { name: /retry alert summary/i })).toBeInTheDocument()
    disconnectedView.unmount()

    vi.useFakeTimers()
    const cached = client({ getAlerts: vi.fn().mockResolvedValue({ items: [], total: 2 }) })
    const cachedView = renderRoute(
      <AlertBanner client={cached} pollingOptions={{ intervalMs: 60_000, staleAfterMs: 5 }} />,
    )
    await act(async () => {})
    expect(screen.getByRole('link', { name: /view 2 runs needing attention/i })).toBeInTheDocument()
    await act(() => vi.advanceTimersByTimeAsync(5))
    expect(screen.getByRole('status')).toHaveTextContent(/alert summary is stale/i)
    cachedView.unmount()
    vi.useRealTimers()
  })

  it.each([
    ['stale', null, /alert summary is stale/i],
    ['disconnected', new ApiError('Offline', { code: 'network_error', retryable: true }), /disconnected.*cached/i],
    ['error', new Error('Rejected'), /unavailable.*cached/i],
  ])('qualifies a cached zero alert total when the resource becomes %s', async (state, failure, expected) => {
    vi.useFakeTimers()
    const getAlerts = failure
      ? vi.fn().mockResolvedValueOnce({ items: [], total: 0 }).mockRejectedValue(failure)
      : vi.fn().mockResolvedValue({ items: [], total: 0 })
    const view = renderRoute(
      <AlertBanner
        client={client({ getAlerts })}
        pollingOptions={{
          intervalMs: failure ? 5 : 60_000,
          staleAfterMs: failure ? 60_000 : 5,
        }}
      />,
    )
    await act(async () => {})
    expect(screen.queryByRole('link', { name: /runs needing attention/i })).not.toBeInTheDocument()
    await act(() => vi.advanceTimersByTimeAsync(5))

    expect(screen.getByRole('status')).toHaveTextContent(expected)
    expect(screen.queryByText(/[1-9]\d* need attention/i)).not.toBeInTheDocument()
    view.unmount()
    vi.useRealTimers()
  })

  it('distinguishes loading, empty, disconnected, stale cached, and application-error states without swallowing failures', async () => {
    const pending = new Promise(() => {})
    const pendingClient = client({ getLeaderboard: vi.fn(() => pending) })
    const view = renderRoute(<Leaderboard client={pendingClient} pollingOptions={{ intervalMs: 60_000 }} />)
    expect(screen.getByRole('status')).toHaveTextContent(/loading completed videos/i)

    const emptyClient = client()
    view.rerender(
      <MemoryRouter>
        <AppProvider loadHealth={false}>
          <Leaderboard client={emptyClient} pollingOptions={{ intervalMs: 60_000 }} />
        </AppProvider>
      </MemoryRouter>,
    )
    expect(await screen.findByText(/no completed videos/i)).toBeInTheDocument()

    const disconnectedClient = client({
      getRevenue: vi.fn().mockRejectedValue(new ApiError('The server could not be reached.', { code: 'network_error', retryable: true })),
    })
    view.rerender(
      <MemoryRouter>
        <AppProvider loadHealth={false}>
          <RevenueDashboard client={disconnectedClient} pollingOptions={{ intervalMs: 60_000 }} />
        </AppProvider>
      </MemoryRouter>,
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(/server could not be reached/i)

    vi.useFakeTimers()
    const staleClient = client({
      getAlerts: vi.fn().mockResolvedValue({
        items: [{ job_id: 'job_alpha', state: 'failed', message: 'Stopped.', created_at: '2026-07-23T10:00:00Z' }],
        total: 1,
      }),
    })
    view.rerender(
      <MemoryRouter>
        <AppProvider loadHealth={false}>
          <AlertList client={staleClient} pollingOptions={{ intervalMs: 60_000, staleAfterMs: 5 }} />
        </AppProvider>
      </MemoryRouter>,
    )
    await act(async () => {})
    await act(() => vi.advanceTimersByTimeAsync(5))
    expect(screen.getByRole('alert')).toHaveTextContent(/stale cached data/i)

    vi.useRealTimers()
    const errorClient = client({ getAggregateCosts: vi.fn().mockRejectedValue(new Error('Cost query rejected.')) })
    view.rerender(
      <MemoryRouter>
        <AppProvider loadHealth={false}>
          <CostDashboard client={errorClient} pollingOptions={{ intervalMs: 60_000 }} />
        </AppProvider>
      </MemoryRouter>,
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(/resource could not be loaded/i)
  })

  it('renders per-job cost zeroes in an accessible table instead of truthy fallbacks', () => {
    render(
      <CostBreakdown costs={[
        { id: 1, category: 'api_tmdb', provider: 'tmdb', amount_usd: 0, units: 0 },
      ]} />,
    )
    const table = screen.getByRole('table', { name: /job cost breakdown/i })
    expect(within(table).getByText('$0.0000')).toBeInTheDocument()
    expect(within(table).getByText('0')).toBeInTheDocument()
    expect(table.parentElement).toHaveClass('overflow-x-auto')
  })
})
