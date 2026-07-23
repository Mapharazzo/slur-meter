import { Component } from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import axe from 'axe-core'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import App, { RouteErrorBoundary } from '../../App'
import { api } from '../../api'
import Sidebar from './Sidebar'
import SystemStatusBar from './SystemStatusBar'

vi.mock('../../api', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    api: {
      ...actual.api,
      health: vi.fn(),
      operationsSummary: vi.fn(),
      listJobs: vi.fn(),
      getAlerts: vi.fn(),
    },
  }
})

beforeEach(() => {
  api.health.mockResolvedValue({ status: 'ok', dispatcher_ready: true })
  api.operationsSummary.mockResolvedValue({ total: 0, states: {} })
  api.listJobs.mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 })
  api.getAlerts.mockResolvedValue({ items: [], total: 0 })
})

function visit(path) {
  window.history.pushState({}, '', path)
  return render(<App poll={false} />)
}

describe('application shell', () => {
  it('starts protected screens locked, unlocks from session context, and locks without protected calls leaking', async () => {
    const user = userEvent.setup()
    visit('/')

    expect(await screen.findByRole('heading', { name: /unlock operations/i })).toBeInTheDocument()
    expect(api.operationsSummary).not.toHaveBeenCalled()
    expect(api.listJobs).not.toHaveBeenCalled()
    expect(api.getAlerts).not.toHaveBeenCalled()
    const token = screen.getByLabelText(/operator token/i)
    await user.type(token, 'session-secret')
    await user.click(screen.getByRole('button', { name: /unlock/i }))
    await waitFor(() => expect(api.operationsSummary).toHaveBeenCalledWith(
      expect.objectContaining({ token: 'session-secret', signal: expect.any(AbortSignal) }),
    ))
    expect(document.body).not.toHaveTextContent('session-secret')
    expect(localStorage.getItem('slur-meter.operator-token')).toBeNull()

    await user.click(screen.getByRole('button', { name: /lock operations/i }))
    expect(screen.getByRole('heading', { name: /unlock operations/i })).toBeInTheDocument()
    expect(sessionStorage.getItem('slur-meter.operator-token')).toBeNull()
  })

  it('loads the authoritative attention total only after unlock and exposes the alert route affordance', async () => {
    api.getAlerts.mockResolvedValue({
      items: [{ job_id: 'job_alpha', state: 'failed', message: 'Stopped.', created_at: '2026-07-23T10:00:00Z' }],
      total: 7,
    })
    const user = userEvent.setup()
    visit('/')
    expect(await screen.findByRole('heading', { name: /unlock operations/i })).toBeInTheDocument()
    expect(api.getAlerts).not.toHaveBeenCalled()

    await user.type(screen.getByLabelText(/operator token/i), 'session-secret')
    await user.click(screen.getByRole('button', { name: /unlock/i }))

    await waitFor(() => expect(api.getAlerts).toHaveBeenCalledWith(
      1,
      { token: 'session-secret', signal: expect.any(AbortSignal) },
    ))
    expect(screen.getByRole('link', { name: /view 7 runs needing attention/i })).toHaveAttribute('href', '/alerts')
  })

  it('provides a skip link, stable main target, landmarks, and current-route semantics', async () => {
    sessionStorage.setItem('slur-meter.operator-token', 'token')
    const { container } = visit('/jobs')

    expect(screen.getByRole('link', { name: /skip to main content/i })).toHaveAttribute('href', '#main-content')
    expect(screen.getByRole('main')).toHaveAttribute('id', 'main-content')
    expect(await screen.findByRole('heading', { level: 1, name: /operations queue/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /operations queue/i })).toHaveAttribute('aria-current', 'page')
    expect((await axe.run(container)).violations).toEqual([])
  })

  it('exposes an accessible responsive drawer toggle and closes on navigation and Escape', async () => {
    const originalWidth = window.innerWidth
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 375 })
    const user = userEvent.setup()
    try {
      render(
        <MemoryRouter initialEntries={['/']}>
          <Sidebar open={false} onClose={vi.fn()} />
        </MemoryRouter>,
      )

      const toggle = screen.getByRole('button', { name: /open navigation/i })
      expect(toggle).toHaveAttribute('aria-expanded', 'false')
      expect(screen.queryByRole('navigation', { name: /primary/i })).not.toBeInTheDocument()
      expect(screen.queryByRole('link', { name: /costs/i })).not.toBeInTheDocument()
      await user.click(toggle)
      expect(screen.getByRole('button', { name: /close navigation/i })).toHaveAttribute('aria-expanded', 'true')
      expect(screen.getByRole('navigation', { name: /primary/i })).toBeVisible()
      await user.keyboard('{Escape}')
      expect(screen.getByRole('button', { name: /open navigation/i })).toHaveAttribute('aria-expanded', 'false')
      expect(screen.queryByRole('navigation', { name: /primary/i })).not.toBeInTheDocument()

      await user.click(toggle)
      await user.click(screen.getByRole('link', { name: /costs/i }))
      expect(screen.getByRole('button', { name: /open navigation/i })).toHaveAttribute('aria-expanded', 'false')
      expect(screen.queryByRole('navigation', { name: /primary/i })).not.toBeInTheDocument()
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth })
    }
  })

  it('keeps primary navigation exposed on desktop even when the mobile drawer is closed', () => {
    const originalWidth = window.innerWidth
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1200 })
    try {
      render(<MemoryRouter><Sidebar open={false} /></MemoryRouter>)
      expect(screen.getByRole('navigation', { name: /primary/i })).toBeInTheDocument()
      expect(screen.getByRole('link', { name: /operations queue/i })).toBeInTheDocument()
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth })
    }
  })

  it.each([
    ['checking', 'loading', /checking system status/i],
    ['ready', 'success', /system ready/i],
    ['dispatcher_unavailable', 'success', /dispatcher unavailable/i],
    ['disconnected', 'disconnected', /disconnected/i],
    ['stale', 'stale', /stale/i],
    ['error', 'error', /system status error/i],
  ])('renders truthful %s system status with text, not color alone', (status, healthStatus, label) => {
    render(
      <SystemStatusBar
        connectivity={{ status }}
        health={{ status: healthStatus, lastSuccessAt: Date.now() - 1_000 }}
      />,
    )
    expect(screen.getByRole('status')).toHaveTextContent(label)
  })

  it('contains route rendering failures behind an actionable boundary', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    class Bomb extends Component {
      render() { throw new Error('secret internal detail') }
    }
    render(<RouteErrorBoundary><Bomb /></RouteErrorBoundary>)

    expect(screen.getByRole('alert')).toHaveTextContent(/screen could not be displayed/i)
    expect(screen.getByRole('button', { name: /try this screen again/i })).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(/secret internal detail/i)
  })

  it('renders a semantic wildcard not-found route without exposing protected data', async () => {
    const { container } = visit('/definitely-missing')

    expect(await screen.findByRole('heading', { name: /page not found/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /return to operations/i })).toHaveAttribute('href', '/')
    expect(api.operationsSummary).not.toHaveBeenCalled()
    expect((await axe.run(container)).violations).toEqual([])
  })
})
