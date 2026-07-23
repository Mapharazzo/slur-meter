import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { AppProvider } from '../../context/AppContext'
import JobDetail from './JobDetail'

vi.mock('../video/VideoPreview', () => ({
  default: ({ jobId, previewAvailable, videoAvailable, compositeAvailable }) => (
    <section aria-label="Media preview">
      {jobId} · preview {String(previewAvailable)} · video {String(videoAvailable)} · composite {String(compositeAvailable)}
    </section>
  ),
}))

const run = {
  id: 'job_alpha', source_imdb_id: 'tt0110912', query: '', label: 'Pulp Fiction',
  state: 'running', current_stage: 'composite', next_action: 'Wait for rendering.',
  safe_error: null, artifact_summary: {}, created_at: '2026-07-23T09:00:00Z',
  updated_at: '2026-07-23T10:00:12Z', started_at: '2026-07-23T10:00:00Z',
  finished_at: null, cancel_requested: false,
}

const parentStage = {
  id: 20, job_id: 'job_alpha', name: 'composite', parent_stage_id: null, ordinal: 7,
  state: 'running', retry_cycle: 2, max_auto_attempts: 3,
  progress: { numerator: 30, denominator: 120, unit: 'frames' },
  started_at: '2026-07-23T10:00:00Z', finished_at: null,
  updated_at: '2026-07-23T10:00:12Z', warnings: ['Preview is delayed.'],
  output_manifest: { frame_count: 30, internal_path: '/srv/private/output' },
  safe_error: null, retryable: false, next_action: 'Continue rendering.',
}

const detail = {
  run,
  stages: [
    { ...parentStage, id: 10, name: 'analysis', ordinal: 5, state: 'completed', progress: { numerator: null, denominator: null, unit: null }, started_at: '2026-07-23T09:58:00Z', finished_at: '2026-07-23T09:59:30Z', warnings: [], next_action: null },
    { ...parentStage, id: 21, name: 'intro_hold', parent_stage_id: 20, ordinal: 2, state: 'completed', progress: { numerator: 24, denominator: 24, unit: 'frames' }, started_at: '2026-07-23T10:00:01Z', finished_at: '2026-07-23T10:00:03Z', warnings: [], next_action: null },
    parentStage,
    { ...parentStage, id: 19, name: 'graph', ordinal: 6, state: 'failed', progress: { numerator: 8, denominator: 10, unit: 'frames' }, safe_error: { code: 'render_failed', message: 'Graph renderer stopped safely.' }, retryable: true, next_action: 'Retry graph after checking fonts.' },
  ],
  attempts: [{ id: 91, job_id: 'job_alpha', stage_id: 19, candidate_id: null, retry_cycle: 2, attempt_number: 3, max_attempts: 3, trigger: 'automatic', started_at: '2026-07-23T09:55:00Z', finished_at: '2026-07-23T09:56:30Z', outcome: 'failed', retryable: true, output: { correlation_id: 'req_safe', echoed: 'Bearer session-token' } }],
  candidates: [], decisions: [], publishing_attempts: [], costs: [], releases: [], revenue: [],
  events: [{ id: 7, job_id: 'job_alpha', stage_id: 20, attempt_id: null, severity: 'info', type: 'progress', message: 'Thirty frames rendered.', data: {}, created_at: '2026-07-23T10:00:12Z' }],
  server_time: '2026-07-23T10:00:13Z', last_event_id: 7,
  available_actions: ['retry_stage:graph'],
}

function client(overrides = {}) {
  return {
    getJob: vi.fn().mockResolvedValue(detail),
    listJobEvents: vi.fn().mockResolvedValue({ items: [], last_event_id: 7 }),
    retryStage: vi.fn().mockResolvedValue({ changed: true }),
    rediscoverSubtitles: vi.fn(), selectSubtitle: vi.fn(), uploadSubtitle: vi.fn(), resumeJob: vi.fn(),
    publish: vi.fn(), retryPublish: vi.fn(), reconcilePublish: vi.fn(), refreshStats: vi.fn(),
    ...overrides,
  }
}

function renderDetail(apiClient = client(), pollingOptions = {}, operatorToken = 'session-token') {
  sessionStorage.setItem('slur-meter.operator-token', operatorToken)
  return {
    apiClient,
    ...render(
      <MemoryRouter initialEntries={['/jobs/job_alpha']}>
        <AppProvider loadHealth={false}>
          <Routes><Route path="/jobs/:jobId" element={<JobDetail client={apiClient} pollingOptions={pollingOptions} />} /></Routes>
        </AppProvider>
      </MemoryRouter>,
    ),
  }
}

beforeEach(() => {
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } })
})

describe('JobDetail operator workspace', () => {
  it('renders the semantic ordered/nested timeline and persisted stage truth on expansion', async () => {
    const user = userEvent.setup()
    renderDetail()

    const timeline = await screen.findByRole('region', { name: /pipeline timeline/i })
    const buttons = within(timeline).getAllByRole('button', { name: /analysis|graph|composite/i }).filter((button) => button.hasAttribute('aria-controls'))
    expect(buttons.map((button) => button.getAttribute('aria-label'))).toEqual([
      expect.stringMatching(/analysis/i), expect.stringMatching(/graph/i), expect.stringMatching(/composite/i),
    ])
    expect(within(timeline).getByText('intro hold').closest('li')).toHaveAttribute('data-parent-stage', '20')
    expect(within(timeline).getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent)).toEqual(expect.arrayContaining(['analysis', 'graph', 'composite', 'intro hold']))

    await user.click(buttons[0])
    const analysisPanel = document.getElementById('stage-10-panel')
    expect(within(analysisPanel).getByText((_text, element) => element.tagName === 'DD' && element.textContent.includes('1 minute 30 seconds'))).toBeInTheDocument()
    expect(within(analysisPanel).getByText('2026-07-23T09:59:30Z').closest('time')).toHaveAttribute('datetime', '2026-07-23T09:59:30Z')

    await user.click(buttons[1])
    const graphPanel = document.getElementById('stage-19-panel')
    expect(screen.getByRole('progressbar', { name: /graph progress/i })).toHaveAttribute('aria-valuenow', '8')
    expect(screen.getByText(/8 of 10 frames/i)).toBeInTheDocument()
    expect(within(graphPanel).getAllByText(/cycle 2/i)).toHaveLength(2)
    const history = screen.getByRole('region', { name: /attempt history/i })
    expect(within(history).getByText(/attempt 3 of 3/i)).toBeInTheDocument()
    expect(within(history).getByText(/automatic/i)).toBeInTheDocument()
    expect(within(history).getByText(/1 minute 30 seconds/i)).toBeInTheDocument()
    expect(screen.getByText(/retry graph after checking fonts/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /retry graph/i })).toBeEnabled()

    await user.click(buttons[2])
    const compositePanel = document.getElementById('stage-20-panel')
    expect(within(compositePanel).getByText(/preview is delayed/i)).toBeInTheDocument()
    expect(within(compositePanel).getByText(/continue rendering/i)).toBeInTheDocument()
  })

  it('keeps technical detail opt-in and copies only bounded public DTO diagnostics', async () => {
    const user = userEvent.setup()
    const writeText = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue(undefined)
    renderDetail()
    await screen.findByRole('heading', { name: /pulp fiction/i })

    expect(screen.queryByText(/req_safe/i)).not.toBeInTheDocument()
    await user.click(screen.getByText(/technical diagnostics/i))
    expect(screen.getByText(/req_safe/i)).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('/srv/private/output')
    await user.click(screen.getByRole('button', { name: /copy sanitized diagnostics/i }))
    expect(writeText).toHaveBeenCalledTimes(1)
    const payload = writeText.mock.calls[0][0]
    expect(payload).toContain('job_alpha')
    expect(payload).not.toContain('session-token')
    expect(payload).not.toContain('Authorization')
    expect(payload).not.toContain('/srv/private/output')
    expect(payload).toContain('[redacted]')
    expect(document.body).not.toHaveTextContent('session-token')
    expect(await screen.findByRole('status')).toHaveTextContent(/diagnostics copied/i)
  })

  it('redacts an opaque operator token when it appears as an arbitrary diagnostic object key', async () => {
    const opaqueToken = 'x7Qp-42'
    const user = userEvent.setup()
    const writeText = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue(undefined)
    const tokenKeyDetail = {
      ...detail,
      attempts: [{ ...detail.attempts[0], output: { [opaqueToken]: 'echoed as a key', safe: 'visible' } }],
    }
    renderDetail(client({ getJob: vi.fn().mockResolvedValue(tokenKeyDetail) }), {}, opaqueToken)

    await user.click(await screen.findByText(/technical diagnostics/i))
    await user.click(screen.getByRole('button', { name: /copy sanitized diagnostics/i }))
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
    const payload = writeText.mock.calls[0][0]
    expect(payload).not.toContain(opaqueToken)
    expect(payload).toContain('[redacted]')
  })

  it('shows a persistent stopping banner and permits only advertised recovery actions with duplicate suppression', async () => {
    let resolveRetry
    const apiClient = client({
      getJob: vi.fn().mockResolvedValue({ ...detail, run: { ...run, state: 'failed', safe_error: { code: 'render_failed', message: 'Rendering stopped.' }, next_action: 'Retry the graph stage.' } }),
      retryStage: vi.fn(() => new Promise((resolve) => { resolveRetry = resolve })),
    })
    const user = userEvent.setup()
    renderDetail(apiClient)

    expect(await screen.findByRole('alert')).toHaveTextContent(/rendering stopped/i)
    const retry = screen.getByRole('button', { name: /retry graph/i })
    await user.click(retry)
    expect(retry).toBeDisabled()
    await user.click(retry)
    expect(apiClient.retryStage).toHaveBeenCalledTimes(1)
    expect(apiClient.retryStage).toHaveBeenCalledWith('job_alpha', 'graph', { token: 'session-token', idempotencyKey: expect.stringMatching(/^ui_/) })
    resolveRetry({ changed: true })
    await waitFor(() => expect(apiClient.getJob).toHaveBeenCalledTimes(2))
    expect(screen.queryByRole('button', { name: /resume/i })).not.toBeInTheDocument()
  })

  it('shares one mutation lock across banner and subtitle controls through durable aggregate refresh settlement', async () => {
    let resolveResume
    let resolveRefresh
    const failedDetail = {
      ...detail,
      run: { ...run, state: 'failed', safe_error: { code: 'stopped', message: 'Run stopped.' } },
      available_actions: ['resume'],
    }
    const refreshResult = { ...detail, run: { ...run, state: 'queued' }, available_actions: ['cancel'] }
    const apiClient = client({
      getJob: vi.fn()
        .mockResolvedValueOnce(failedDetail)
        .mockImplementationOnce(() => new Promise((resolve) => { resolveRefresh = () => resolve(refreshResult) }))
        .mockResolvedValue(refreshResult),
      resumeJob: vi.fn(() => new Promise((resolve) => { resolveResume = resolve })),
    })
    const user = userEvent.setup()
    renderDetail(apiClient)
    const resumeButtons = await screen.findAllByRole('button', { name: /resume run/i })

    await user.click(resumeButtons[0])
    expect(resumeButtons.every((button) => button.disabled)).toBe(true)
    await user.click(resumeButtons[1])
    expect(apiClient.resumeJob).toHaveBeenCalledTimes(1)
    resolveResume({ changed: true })
    await waitFor(() => expect(apiClient.getJob).toHaveBeenCalledTimes(2))
    expect(resumeButtons.every((button) => button.disabled)).toBe(true)
    const manualRefresh = screen.getByRole('button', { name: /refresh job/i })
    expect(manualRefresh).toBeDisabled()
    await user.click(manualRefresh)
    expect(apiClient.getJob).toHaveBeenCalledTimes(2)
    resolveRefresh()
    await waitFor(() => expect(screen.queryByRole('button', { name: /resume run/i })).not.toBeInTheDocument())
  })

  it('surfaces delegated subtitle and publishing failures inside the initiating panel', async () => {
    const candidate = {
      id: 'cand_1', job_id: 'job_alpha', rank: 1, provider: 'opensubs', source_type: 'provider',
      provider_id: 'provider_1', provider_filename: 'movie.srt', title: 'Pulp Fiction', year: 1994,
      imdb_match: true, language: 'en', parsed_duration_seconds: 100, expected_runtime_seconds: 100,
      coverage_percent: 100, status: 'accepted', quality_reasons: [], rejection_reasons: [],
      rank_reasons: [], selection_method: null,
    }
    const actionDetail = {
      ...detail,
      stages: [...detail.stages, { ...parentStage, id: 40, name: 'subtitle_selection', parent_stage_id: null, ordinal: 4, state: 'needs_attention', progress: { numerator: null, denominator: null, unit: null }, warnings: [], next_action: null }],
      candidates: [candidate],
      available_actions: ['select_subtitle:cand_1', 'publish:youtube'],
    }
    const apiClient = client({
      getJob: vi.fn().mockResolvedValue(actionDetail),
      selectSubtitle: vi.fn().mockRejectedValue(new Error('Subtitle selection failed safely.')),
      publish: vi.fn().mockRejectedValue(new Error('Publishing failed safely.')),
    })
    const user = userEvent.setup()
    renderDetail(apiClient)

    const subtitles = await screen.findByRole('region', { name: /subtitle candidates/i })
    await user.click(within(subtitles).getByRole('button', { name: /select candidate/i }))
    expect(await within(subtitles).findByRole('alert')).toHaveTextContent(/subtitle selection failed safely/i)
    expect(screen.getAllByText(/subtitle selection failed safely/i)).toHaveLength(1)

    const publishing = screen.getByRole('region', { name: /^publishing$/i })
    await user.click(within(publishing).getByRole('button', { name: /publish youtube/i }))
    expect(await within(publishing).findByRole('alert')).toHaveTextContent(/publishing failed safely/i)
    expect(screen.getAllByText(/publishing failed safely/i)).toHaveLength(1)
  })

  it('collapses the pipeline timeline once the run is completed', async () => {
    renderDetail(client({ getJob: vi.fn().mockResolvedValue({ ...detail, run: { ...run, state: 'completed' } }) }))

    const summary = await screen.findByText(/all stages complete/i)
    expect(summary.closest('details')).not.toHaveAttribute('open')
    expect(screen.queryByRole('region', { name: /pipeline timeline/i })).toBeNull()
  })

  it('collapses the subtitle section into an expander once selection is resolved', async () => {
    const resolvedDetail = {
      ...detail,
      stages: [...detail.stages, { ...parentStage, id: 41, name: 'subtitle_selection', parent_stage_id: null, ordinal: 4, state: 'completed', progress: { numerator: null, denominator: null, unit: null }, warnings: [], next_action: null }],
      candidates: [{ id: 'cand_1', job_id: 'job_alpha', rank: 1, provider: 'opensubtitles', source_type: 'download', provider_filename: 'chosen.srt', status: 'selected', coverage_percent: 95.4, expected_runtime_seconds: 9060, parsed_duration_seconds: 8900, rank_reasons: [], quality_reasons: [], rejection_reasons: [], selection_method: 'automatic' }],
      available_actions: [],
    }
    renderDetail(client({ getJob: vi.fn().mockResolvedValue(resolvedDetail) }))

    await screen.findByRole('region', { name: /pipeline timeline/i })
    // No prominent subtitle region — it is tucked into a collapsed expander.
    expect(screen.queryByRole('region', { name: /subtitle candidates/i })).toBeNull()
    const summary = screen.getByText(/expand to review or override/i)
    expect(summary.closest('details')).not.toHaveAttribute('open')
    expect(summary).toHaveTextContent(/using #1/i)
    expect(summary).toHaveTextContent(/95% coverage/i)
  })

  it('surfaces the subtitle section prominently when selection needs an operator', async () => {
    const attentionDetail = {
      ...detail,
      run: { ...run, state: 'needs_attention', current_stage: 'subtitle_selection' },
      stages: [...detail.stages, { ...parentStage, id: 42, name: 'subtitle_selection', parent_stage_id: null, ordinal: 4, state: 'needs_attention', progress: { numerator: null, denominator: null, unit: null }, warnings: [], next_action: null }],
      candidates: [{ id: 'cand_1', job_id: 'job_alpha', rank: 1, provider: 'opensubtitles', source_type: 'download', provider_filename: 'a.srt', status: 'rejected', coverage_percent: null, expected_runtime_seconds: null, parsed_duration_seconds: null, rank_reasons: [], quality_reasons: [], rejection_reasons: ['expected_runtime_unavailable'], selection_method: null }],
      available_actions: ['select_subtitle:cand_1', 'rediscover_subtitles'],
    }
    renderDetail(client({ getJob: vi.fn().mockResolvedValue(attentionDetail) }))

    expect(await screen.findByRole('region', { name: /subtitle candidates/i })).toBeInTheDocument()
    expect(screen.queryByText(/expand to review or override/i)).toBeNull()
  })

  it('mounts media preview with canonical identity and durable manifest availability', async () => {
    const mediaDetail = {
      ...detail,
      stages: [
        ...detail.stages,
        { ...parentStage, id: 30, name: 'encode', state: 'completed', output_manifest: { details: { final_file: 'final.mp4' } } },
      ].map((stage) => (
        stage.name === 'graph'
          ? { ...stage, state: 'completed', output_manifest: { details: { preview_file: 'preview.png' } } }
          : stage.name === 'composite'
            ? { ...stage, state: 'completed', output_manifest: { details: { timing: { graph: { num_frames: 10 } } } } }
            : stage
      )),
    }
    renderDetail(client({ getJob: vi.fn().mockResolvedValue(mediaDetail) }))

    expect(await screen.findByRole('region', { name: /media preview/i })).toHaveTextContent(
      /job_alpha.*preview true.*video true.*composite true/i,
    )
  })

  it('does not advertise media for completed stages with empty manifests', async () => {
    const noMediaDetail = {
      ...detail,
      stages: detail.stages.map((stage) => (
        ['graph', 'composite'].includes(stage.name)
          ? { ...stage, state: 'completed', output_manifest: {} }
          : stage
      )).concat({
        ...parentStage,
        id: 31,
        name: 'encode',
        state: 'completed',
        output_manifest: {},
      }),
    }
    renderDetail(client({ getJob: vi.fn().mockResolvedValue(noMediaDetail) }))

    await screen.findByRole('heading', { name: /pulp fiction/i })
    expect(screen.queryByRole('region', { name: /media preview/i })).not.toBeInTheDocument()
  })

  it('merges incremental events monotonically without duplicates and stops active polling on a terminal snapshot while retaining manual refresh', async () => {
    const terminalEvents = [
      ...detail.events,
      { ...detail.events[0], id: 8, message: 'Composite advanced.' },
      { ...detail.events[0], id: 9, message: 'Run completed.' },
    ]
    const terminal = { ...detail, run: { ...run, state: 'completed', finished_at: '2026-07-23T10:02:00Z' }, events: terminalEvents, last_event_id: 9 }
    const apiClient = client({
      getJob: vi.fn().mockResolvedValueOnce(detail).mockResolvedValue(terminal),
      listJobEvents: vi.fn().mockResolvedValue({ items: terminalEvents.slice(1), last_event_id: 9 }),
    })
    renderDetail(apiClient, { intervalMs: 20, staleAfterMs: 60_000 })
    await waitFor(() => expect(apiClient.getJob).toHaveBeenCalledWith('job_alpha', { token: 'session-token', signal: expect.any(AbortSignal) }))
    await waitFor(() => expect(apiClient.getJob).toHaveBeenCalledTimes(2))
    await act(() => new Promise((resolve) => setTimeout(resolve, 100)))
    expect(apiClient.getJob).toHaveBeenCalledTimes(2)
    expect(apiClient.listJobEvents.mock.calls.some((call) => call[1].after === 7)).toBe(true)
    expect(screen.getAllByText(/composite advanced|run completed/i)).toHaveLength(2)

    await userEvent.setup().click(screen.getByRole('button', { name: /refresh job/i }))
    await waitFor(() => expect(apiClient.getJob).toHaveBeenCalledTimes(3))
  })

  it('never overlaps incremental event requests and aborts/fences stale identity responses', async () => {
    let resolveAlphaEvents
    const eventSignals = []
    const alphaEvents = new Promise((resolve) => { resolveAlphaEvents = resolve })
    const betaDetail = { ...detail, run: { ...run, id: 'job_beta', label: 'Beta movie' }, stages: detail.stages.map((stage) => ({ ...stage, job_id: 'job_beta' })), events: [], last_event_id: 0 }
    const apiClient = client({
      getJob: vi.fn((id) => Promise.resolve(id === 'job_beta' ? betaDetail : detail)),
      listJobEvents: vi.fn((id, _params, options) => {
        eventSignals.push({ id, signal: options.signal })
        return id === 'job_alpha' ? alphaEvents : new Promise(() => {})
      }),
    })
    sessionStorage.setItem('slur-meter.operator-token', 'session-token')
    const user = userEvent.setup()
    const view = render(
      <MemoryRouter initialEntries={['/jobs/job_alpha']}>
        <AppProvider loadHealth={false}>
          <Link to="/jobs/job_beta">Next job</Link>
          <Routes><Route path="/jobs/:jobId" element={<JobDetail client={apiClient} pollingOptions={{ intervalMs: 10_000, eventIntervalMs: 10 }} />} /></Routes>
        </AppProvider>
      </MemoryRouter>,
    )
    await screen.findByRole('heading', { name: /pulp fiction/i })
    await act(() => new Promise((resolve) => setTimeout(resolve, 40)))
    expect(apiClient.listJobEvents).toHaveBeenCalledTimes(1)

    await user.click(screen.getByRole('link', { name: /next job/i }))
    expect(await screen.findByRole('heading', { name: /beta movie/i })).toBeInTheDocument()
    await waitFor(() => expect(eventSignals.some(({ id }) => id === 'job_beta')).toBe(true))
    expect(eventSignals.find(({ id }) => id === 'job_alpha').signal.aborted).toBe(true)
    resolveAlphaEvents({ items: [{ ...detail.events[0], id: 99, message: 'Stale alpha event.' }], last_event_id: 99 })
    await act(async () => {})
    expect(screen.queryByText(/stale alpha event/i)).not.toBeInTheDocument()
    view.unmount()
    expect(eventSignals.find(({ id }) => id === 'job_beta').signal.aborted).toBe(true)
  })
})
