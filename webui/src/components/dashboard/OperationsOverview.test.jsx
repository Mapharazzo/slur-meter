import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import axe from 'axe-core'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { ApiError } from '../../api'
import { AppProvider } from '../../context/AppContext'
import OperationsOverview from './OperationsOverview'

function job(id, state, overrides = {}) {
  return {
    id,
    source_imdb_id: `tt${id.replace(/\D/g, '').padStart(7, '0')}`,
    label: `Movie ${id}`,
    state,
    current_stage: state === 'running' ? 'encode' : null,
    next_action: state === 'needs_attention' ? 'Select a subtitle candidate.' : null,
    safe_error: state === 'failed' ? { message: 'Provider could not be reached.' } : null,
    created_at: '2026-07-23T10:00:00Z',
    updated_at: '2026-07-23T10:05:00Z',
    ...overrides,
  }
}

const allJobs = [
  job('job_completed', 'completed'),
  job('job_failed', 'failed', { next_action: 'Retry after checking the provider.' }),
  job('job_running', 'running'),
  job('job_queued', 'queued'),
  job('job_attention', 'needs_attention'),
]

function client(overrides = {}) {
  return {
    operationsSummary: vi.fn().mockResolvedValue({
      total: 12,
      states: {
        running: 2,
        needs_attention: 1,
        failed: 0,
        queued: 4,
        completed: 5,
        paused_by_future_worker: 0,
      },
    }),
    listJobs: vi.fn().mockResolvedValue({ items: allJobs, total: 5, limit: 20, offset: 0 }),
    submitJob: vi.fn(),
    ...overrides,
  }
}

function LocationProbe() {
  const location = useLocation()
  const navigate = useNavigate()
  return (
    <>
      <output aria-label="Current location">{location.pathname}{location.search}</output>
      <button type="button" onClick={() => navigate('/?query=new&page=9')}>Navigate to high page</button>
    </>
  )
}

function renderOverview({ apiClient = client(), route = '/', poll = false, pollingOptions } = {}) {
  return {
    apiClient,
    ...render(
      <MemoryRouter initialEntries={[route]}>
        <AppProvider loadHealth={false}>
          <OperationsOverview client={apiClient} poll={poll} pollingOptions={pollingOptions} />
          <LocationProbe />
        </AppProvider>
      </MemoryRouter>,
    ),
  }
}

describe('OperationsOverview', () => {
  it('groups durable states in operational priority order and links by canonical job id', async () => {
    const { container } = renderOverview()

    await screen.findByRole('heading', { level: 2, name: 'Active' })
    const groupNames = ['Active', 'Needs attention', 'Failed', 'Queued', 'Recently completed']
    expect(groupNames.map((name) => screen.getByRole('heading', { level: 2, name }).textContent)).toEqual([
      expect.stringMatching(/active/i),
      expect.stringMatching(/needs attention/i),
      expect.stringMatching(/failed/i),
      expect.stringMatching(/queued/i),
      expect.stringMatching(/recently completed/i),
    ])
    expect(screen.getByRole('link', { name: /movie job_running/i })).toHaveAttribute('href', '/jobs/job_running')
    expect(screen.getByText('Select a subtitle candidate.')).toBeInTheDocument()
    expect(screen.getByText('Provider could not be reached.')).toBeInTheDocument()
    expect(screen.getByText('Retry after checking the provider.')).toBeInTheDocument()
    expect(container).not.toHaveTextContent(/fetching|rendering|\bdone\b/i)
    expect((await axe.run(container)).violations).toEqual([])
  })

  it('renders authoritative global counts separately from bounded page counts, including zero and unknown states', async () => {
    renderOverview()

    const summary = await screen.findByRole('region', { name: /operations summary/i })
    expect(within(summary).getByText('12')).toBeInTheDocument()
    expect(within(summary).getByText('running').nextElementSibling).toHaveTextContent('2')
    expect(within(summary).getByText('failed').nextElementSibling).toHaveTextContent('0')
    expect(within(summary).getByText('paused by future worker').nextElementSibling).toHaveTextContent('0')
    expect(screen.getByText(/showing 5 of 5 matching runs/i)).toBeInTheDocument()
  })

  it('owns search, durable state filter, and page in the URL and sends bounded API queries', async () => {
    const user = userEvent.setup()
    const apiClient = client({
      listJobs: vi.fn().mockResolvedValue({ items: [], total: 41, limit: 20, offset: 20 }),
    })
    renderOverview({ apiClient, route: '/?query=pulp&state=failed&page=2' })

    await waitFor(() => expect(apiClient.listJobs).toHaveBeenCalledWith(
      { query: 'pulp', state: 'failed', limit: 20, offset: 20 },
      expect.objectContaining({ token: expect.any(String), signal: expect.any(AbortSignal) }),
    ))
    expect(screen.getByRole('searchbox', { name: /search runs/i })).toHaveValue('pulp')
    expect(screen.getByRole('combobox', { name: /state filter/i })).toHaveValue('failed')
    expect(screen.getByRole('button', { name: /previous page/i })).toBeEnabled()

    await user.clear(screen.getByRole('searchbox', { name: /search runs/i }))
    await user.type(screen.getByRole('searchbox', { name: /search runs/i }), 'casino')
    await user.click(screen.getByRole('button', { name: /apply filters/i }))
    expect(screen.getByLabelText(/current location/i)).toHaveTextContent('query=casino')
    expect(screen.getByLabelText(/current location/i)).toHaveTextContent('state=failed')
    expect(screen.getByLabelText(/current location/i)).not.toHaveTextContent('page=2')
  })

  it('sanitizes invalid URL filter and page before loading', async () => {
    const apiClient = client({ listJobs: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 }) })
    renderOverview({ apiClient, route: '/?state=done&page=-400&query=%20%20' })

    await waitFor(() => expect(apiClient.listJobs).toHaveBeenCalledWith(
      { limit: 20, offset: 0 },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ))
    expect(screen.getByLabelText(/current location/i)).toHaveTextContent(/^\/$/)
  })

  it('canonicalizes and refetches a server-known out-of-range page', async () => {
    const apiClient = client({
      listJobs: vi.fn()
        .mockResolvedValueOnce({ items: [], total: 21, limit: 20, offset: 160 })
        .mockResolvedValue({ items: [job('job_last', 'completed')], total: 21, limit: 20, offset: 20 }),
    })
    renderOverview({ apiClient, route: '/?query=pulp&page=9' })

    await waitFor(() => expect(apiClient.listJobs).toHaveBeenCalledWith(
      { query: 'pulp', limit: 20, offset: 20 },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ))
    expect(screen.getByLabelText(/current location/i)).toHaveTextContent('/?query=pulp&page=2')
    expect(await screen.findByText(/page 2 of 2/i)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /movie job_last/i })).toBeInTheDocument()
  })

  it('does not clamp a restored URL with cached totals from the previous query', async () => {
    const user = userEvent.setup()
    const listJobs = vi.fn(async (params) => {
      if (params.query === 'old') {
        return { items: [job('job_old', 'completed')], total: 21, limit: 20, offset: params.offset }
      }
      return { items: [job('job_new', 'queued')], total: 200, limit: 20, offset: params.offset }
    })
    const apiClient = client({ listJobs })
    renderOverview({ apiClient, route: '/?query=old&page=2' })
    await screen.findByRole('link', { name: /movie job_old/i })

    await user.click(screen.getByRole('button', { name: /navigate to high page/i }))

    await waitFor(() => expect(listJobs).toHaveBeenCalledWith(
      { query: 'new', limit: 20, offset: 160 },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ))
    expect(screen.getByLabelText(/current location/i)).toHaveTextContent('/?query=new&page=9')
    expect(await screen.findByRole('link', { name: /movie job_new/i })).toBeInTheDocument()
    expect(listJobs).not.toHaveBeenCalledWith(
      { query: 'new', limit: 20, offset: 20 },
      expect.anything(),
    )
  })

  it('distinguishes initial loading from genuinely empty results', async () => {
    let resolveSummary
    let resolveJobs
    const apiClient = client({
      operationsSummary: vi.fn(() => new Promise((resolve) => { resolveSummary = resolve })),
      listJobs: vi.fn(() => new Promise((resolve) => { resolveJobs = resolve })),
    })
    renderOverview({ apiClient })

    expect(screen.getAllByRole('status').some((node) => /loading/i.test(node.textContent))).toBe(true)
    await waitFor(() => expect(resolveSummary).toBeTypeOf('function'))
    resolveSummary({ total: 0, states: {} })
    resolveJobs({ items: [], total: 0, limit: 20, offset: 0 })
    expect(await screen.findByText(/no runs match this view/i)).toBeInTheDocument()
  })

  it.each([
    ['error', 'Queue request failed.', /queue request failed/i],
    ['disconnected', 'Server is offline.', /server could not be reached/i],
  ])('shows a distinct %s state without claiming the queue is empty', async (_kind, message, pattern) => {
    const error = _kind === 'disconnected'
      ? Object.assign(new TypeError(message), { message })
      : new ApiError(message, { code: 'queue_failed' })
    const apiClient = client({ listJobs: vi.fn().mockRejectedValue(error) })
    renderOverview({ apiClient })

    expect(await screen.findByRole('alert')).toHaveTextContent(pattern)
    expect(screen.queryByText(/no runs match/i)).not.toBeInTheDocument()
  })

  it('keeps cached rows visible beneath stale, disconnected, and API-error warnings', async () => {
    const user = userEvent.setup()
    const apiClient = client()
    renderOverview({ apiClient, pollingOptions: { staleAfterMs: 20 } })
    await screen.findByRole('link', { name: /movie job_running/i })

    const staleWarnings = await screen.findAllByRole('alert', {}, { timeout: 1_000 })
    expect(staleWarnings.some((warning) => /stale cached data/i.test(warning.textContent))).toBe(true)
    expect(screen.getByRole('link', { name: /movie job_running/i })).toBeInTheDocument()

    apiClient.listJobs.mockRejectedValueOnce(new TypeError('Connection unavailable.'))
    await user.click(screen.getByRole('button', { name: /refresh queue/i }))
    await waitFor(() => expect(
      screen.getAllByRole('alert').some((warning) => /connection unavailable|last received/i.test(warning.textContent)),
    ).toBe(true))
    expect(screen.getByRole('link', { name: /movie job_running/i })).toBeInTheDocument()

    apiClient.listJobs.mockRejectedValueOnce(new ApiError('Safe API failure.', { code: 'safe_failure' }))
    await user.click(screen.getByRole('button', { name: /refresh queue/i }))
    await waitFor(() => expect(
      screen.getAllByRole('alert').some((warning) => /safe api failure|last received/i.test(warning.textContent)),
    ).toBe(true))
    expect(screen.getByRole('link', { name: /movie job_running/i })).toBeInTheDocument()
  })

  it('submits exactly one labelled mode with one idempotency key, blocks duplicates, and navigates by canonical id', async () => {
    let resolveSubmit
    const submitJob = vi.fn(() => new Promise((resolve) => { resolveSubmit = resolve }))
    const user = userEvent.setup()
    const apiClient = client({ submitJob })
    renderOverview({ apiClient })

    const input = await screen.findByRole('textbox', { name: /movie title or query/i })
    await user.type(input, 'Pulp Fiction')
    await user.click(screen.getByRole('button', { name: /create run/i }))
    expect(screen.getByRole('button', { name: /creating run/i })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: /creating run/i }))
    expect(submitJob).toHaveBeenCalledTimes(1)
    expect(submitJob).toHaveBeenCalledWith(
      { query: 'Pulp Fiction' },
      { token: '', idempotencyKey: expect.stringMatching(/^ui_/) },
    )

    resolveSubmit(job('job_canonical', 'queued', { source_imdb_id: 'tt0110912' }))
    await waitFor(() => expect(screen.getByLabelText(/current location/i)).toHaveTextContent('/jobs/job_canonical'))
  })

  it('shows inline validation and safe API submission failures without alert()', async () => {
    const user = userEvent.setup()
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {})
    const apiClient = client({ submitJob: vi.fn().mockRejectedValue(new Error('Submission was rejected.')) })
    renderOverview({ apiClient })

    await user.click(await screen.findByRole('button', { name: /create run/i }))
    expect(screen.getByText(/enter one movie title or canonical imdb id/i)).toBeInTheDocument()
    await user.click(screen.getByRole('radio', { name: /canonical imdb id/i }))
    await user.type(screen.getByRole('textbox', { name: /canonical imdb id/i }), 'not-an-id')
    await user.click(screen.getByRole('button', { name: /create run/i }))
    expect(screen.getByText(/use a canonical imdb id/i)).toBeInTheDocument()

    await user.clear(screen.getByRole('textbox', { name: /canonical imdb id/i }))
    await user.type(screen.getByRole('textbox', { name: /canonical imdb id/i }), 'tt0110912')
    await user.click(screen.getByRole('button', { name: /create run/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/submission was rejected/i)
    expect(alertSpy).not.toHaveBeenCalled()
  })
})
