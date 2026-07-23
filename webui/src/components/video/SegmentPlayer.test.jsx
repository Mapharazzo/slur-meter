import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../../api'
import { AppProvider } from '../../context/AppContext'
import FrameBrowser from './FrameBrowser'
import SegmentPlayer from './SegmentPlayer'
import VideoPreview from './VideoPreview'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function renderWithSession(ui, token = 'media-token') {
  sessionStorage.setItem('slur-meter.operator-token', token)
  return render(<AppProvider loadHealth={false}>{ui}</AppProvider>)
}

beforeEach(() => {
  vi.spyOn(URL, 'createObjectURL').mockImplementation((blob) => `blob:${blob.size}:${Math.random()}`)
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
})

describe('authenticated identity-safe media', () => {
  it('resets run and segment state, aborts old work, and suppresses late segment/frame results', async () => {
    const oldInfo = deferred()
    const oldFrame = deferred()
    const signals = []
    vi.spyOn(api, 'getSegmentInfo').mockImplementation((jobId, segment, options) => {
      signals.push({ kind: 'info', jobId, segment, signal: options.signal })
      return jobId === 'job_old' ? oldInfo.promise : Promise.resolve({
        segment: 'verdict', frame_count: 2, fps: 24, timing: {},
      })
    })
    vi.spyOn(api, 'fetchFrame').mockImplementation((jobId, segment, frame, options) => {
      signals.push({ kind: 'frame', jobId, segment, frame, signal: options.signal })
      return jobId === 'job_old' ? oldFrame.promise : Promise.resolve(new Blob(['new-frame']))
    })

    const view = renderWithSession(<SegmentPlayer jobId="job_old" segment="graph" />)
    expect(screen.getByRole('status')).toHaveTextContent(/loading graph segment/i)
    oldInfo.resolve({ segment: 'graph', frame_count: 3, fps: 12, timing: {} })
    await waitFor(() => expect(api.fetchFrame).toHaveBeenCalledWith(
      'job_old', 'graph', 0, { token: 'media-token', signal: expect.any(AbortSignal) },
    ))

    view.rerender(
      <AppProvider loadHealth={false}>
        <SegmentPlayer jobId="job_new" segment="verdict" />
      </AppProvider>,
    )
    expect(signals.find((call) => call.jobId === 'job_old' && call.kind === 'frame').signal.aborted).toBe(true)
    expect(await screen.findByRole('img', { name: /verdict frame 1 of 2/i })).toHaveAttribute('src', expect.stringMatching(/^blob:/))
    oldFrame.resolve(new Blob(['late-old-frame']))
    await act(async () => {})

    expect(screen.getByRole('img', { name: /verdict frame 1 of 2/i })).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: /graph frame/i })).not.toBeInTheDocument()
  })

  it('uses backend FPS for scheduling, has labelled truthful controls, and cleans URLs on unmount', async () => {
    const callbacks = []
    vi.stubGlobal('requestAnimationFrame', vi.fn((callback) => {
      callbacks.push(callback)
      return callbacks.length
    }))
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
    vi.spyOn(api, 'getSegmentInfo').mockResolvedValue({
      segment: 'graph', frame_count: 3, fps: 10, timing: {},
    })
    vi.spyOn(api, 'fetchFrame').mockImplementation((_job, _segment, frame) => Promise.resolve(new Blob([`frame-${frame}`])))

    const user = userEvent.setup()
    const view = renderWithSession(<SegmentPlayer jobId="job_alpha" segment="graph" />)
    expect(await screen.findByRole('img', { name: /graph frame 1 of 3/i })).toBeInTheDocument()
    const player = screen.getByRole('region', { name: /graph segment player/i })
    const play = within(player).getByRole('button', { name: /play graph segment/i })
    expect(within(player).getByText('1 / 3')).toBeInTheDocument()
    await user.click(play)
    expect(within(player).getByRole('button', { name: /pause graph segment/i })).toBeInTheDocument()

    act(() => callbacks.shift()(0))
    act(() => callbacks.shift()(99))
    expect(within(player).getByText('1 / 3')).toBeInTheDocument()
    act(() => callbacks.shift()(100))
    await waitFor(() => expect(within(player).getByText('2 / 3')).toBeInTheDocument())
    expect(api.fetchFrame).toHaveBeenLastCalledWith(
      'job_alpha', 'graph', 1, { token: 'media-token', signal: expect.any(AbortSignal) },
    )

    view.unmount()
    expect(cancelAnimationFrame).toHaveBeenCalled()
    expect(URL.revokeObjectURL).toHaveBeenCalled()
  })

  it('shows explicit segment and frame failures instead of an empty surface', async () => {
    vi.spyOn(api, 'getSegmentInfo').mockRejectedValueOnce(new Error('Segment metadata unavailable.'))
    const view = renderWithSession(<SegmentPlayer jobId="job_alpha" segment="graph" />)
    expect(await screen.findByRole('alert')).toHaveTextContent(/segment metadata unavailable/i)
    view.unmount()

    vi.restoreAllMocks()
    vi.spyOn(URL, 'createObjectURL').mockImplementation(() => 'blob:frame')
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    vi.spyOn(api, 'getSegmentInfo').mockResolvedValue({ segment: 'graph', frame_count: 1, fps: 24, timing: {} })
    vi.spyOn(api, 'fetchFrame').mockRejectedValue(new Error('Frame unavailable.'))
    renderWithSession(<FrameBrowser jobId="job_alpha" segment="graph" />)
    expect(await screen.findByRole('alert')).toHaveTextContent(/frame unavailable/i)
  })

  it('browses authenticated blob frames with labelled controls and revokes replacements', async () => {
    vi.spyOn(api, 'getSegmentInfo').mockResolvedValue({ segment: 'verdict', frame_count: 2, fps: 24, timing: {} })
    vi.spyOn(api, 'fetchFrame').mockImplementation((_job, _segment, frame) => Promise.resolve(new Blob([`frame-${frame}`])))
    const user = userEvent.setup()
    const view = renderWithSession(<FrameBrowser jobId="job_alpha" segment="verdict" />)

    const browser = await screen.findByRole('region', { name: /verdict frame browser/i })
    expect(within(browser).getByRole('button', { name: /previous frame/i })).toBeDisabled()
    expect(within(browser).getByRole('slider', { name: /select verdict frame/i })).toHaveAttribute('aria-valuetext', 'Frame 1 of 2')
    await user.click(within(browser).getByRole('button', { name: /next frame/i }))
    expect(await within(browser).findByRole('img', { name: /verdict frame 2 of 2/i })).toBeInTheDocument()
    expect(URL.revokeObjectURL).toHaveBeenCalled()
    expect(api.fetchFrame).toHaveBeenLastCalledWith(
      'job_alpha', 'verdict', 1, { token: 'media-token', signal: expect.any(AbortSignal) },
    )
    view.unmount()
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(2)
  })

  it('fetches preview and final video with auth, keeps raw URLs credential-free, and renders only durable availability', async () => {
    vi.spyOn(api, 'fetchPreview').mockResolvedValue(new Blob(['preview']))
    vi.spyOn(api, 'fetchVideo').mockResolvedValue(new Blob(['video']))
    vi.spyOn(api, 'getSegmentInfo').mockResolvedValue({ segment: 'graph', frame_count: 0, fps: 24, timing: {} })
    const user = userEvent.setup()
    renderWithSession(
      <VideoPreview
        jobId="job_alpha"
        previewAvailable
        videoAvailable
        compositeAvailable
        segmentTiming={{ graph: { start_time: 0, end_time: 1 } }}
      />,
    )

    expect(await screen.findByRole('img', { name: /latest graph preview/i })).toHaveAttribute('src', expect.stringMatching(/^blob:/))
    expect(api.fetchPreview).toHaveBeenCalledWith('job_alpha', { token: 'media-token', signal: expect.any(AbortSignal) })
    await user.click(screen.getByRole('button', { name: /final video/i }))
    expect(await screen.findByLabelText(/final video for job_alpha/i)).toHaveAttribute('src', expect.stringMatching(/^blob:/))
    expect(api.fetchVideo).toHaveBeenCalledWith('job_alpha', { token: 'media-token', signal: expect.any(AbortSignal) })
    const download = screen.getByRole('button', { name: /download final mp4/i })
    await user.click(download)
    expect(document.body.innerHTML).not.toContain('media-token')
    expect(api.videoUrl('job_alpha')).toBe('/api/videos/job_alpha')
    expect(api.previewUrl('job_alpha')).toBe('/api/jobs/job_alpha/preview')
  })

  it('preserves the operator media selection across equivalent polling snapshots', async () => {
    vi.spyOn(api, 'fetchPreview').mockResolvedValue(new Blob(['preview']))
    vi.spyOn(api, 'fetchVideo').mockResolvedValue(new Blob(['video']))
    const props = {
      jobId: 'job_alpha',
      previewAvailable: true,
      videoAvailable: true,
      compositeAvailable: true,
    }
    const user = userEvent.setup()
    const view = renderWithSession(
      <VideoPreview {...props} segmentTiming={{ graph: { start_time: 0, end_time: 1 } }} />,
    )
    await screen.findByRole('img', { name: /latest graph preview/i })
    await user.click(screen.getByRole('button', { name: /final video/i }))
    expect(await screen.findByLabelText(/final video for job_alpha/i)).toBeInTheDocument()

    view.rerender(
      <AppProvider loadHealth={false}>
        <VideoPreview {...props} segmentTiming={{ graph: { start_time: 0, end_time: 1 } }} />
      </AppProvider>,
    )

    expect(screen.getByLabelText(/final video for job_alpha/i)).toBeInTheDocument()
  })

  it('aborts and suppresses late preview/video blobs and revokes URLs on tab, identity, and unmount', async () => {
    const alphaPreview = deferred()
    const alphaVideo = deferred()
    const betaPreview = deferred()
    const signals = []
    vi.spyOn(api, 'fetchPreview').mockImplementation((jobId, options) => {
      signals.push({ kind: 'preview', jobId, signal: options.signal })
      return jobId === 'job_alpha' ? alphaPreview.promise : betaPreview.promise
    })
    vi.spyOn(api, 'fetchVideo').mockImplementation((jobId, options) => {
      signals.push({ kind: 'video', jobId, signal: options.signal })
      return alphaVideo.promise
    })
    const user = userEvent.setup()
    const view = renderWithSession(
      <VideoPreview jobId="job_alpha" previewAvailable videoAvailable />,
    )
    alphaPreview.resolve(new Blob(['alpha-preview']))
    expect(await screen.findByRole('img', { name: /latest graph preview/i })).toHaveAttribute('src', expect.stringMatching(/^blob:/))
    await user.click(screen.getByRole('button', { name: /final video/i }))
    await waitFor(() => expect(signals.some(({ kind }) => kind === 'video')).toBe(true))
    await user.click(screen.getByRole('button', { name: /graph preview/i }))
    expect(signals.find(({ kind }) => kind === 'video').signal.aborted).toBe(true)
    const objectUrlCalls = URL.createObjectURL.mock.calls.length
    alphaVideo.resolve(new Blob(['late-video']))
    await act(async () => {})
    expect(URL.createObjectURL).toHaveBeenCalledTimes(objectUrlCalls)

    view.rerender(
      <AppProvider loadHealth={false}>
        <VideoPreview jobId="job_beta" previewAvailable />
      </AppProvider>,
    )
    expect(URL.revokeObjectURL).toHaveBeenCalled()
    betaPreview.resolve(new Blob(['beta-preview']))
    expect(await screen.findByRole('img', { name: /latest graph preview/i })).toBeInTheDocument()
    const revokeCalls = URL.revokeObjectURL.mock.calls.length
    view.unmount()
    expect(URL.revokeObjectURL.mock.calls.length).toBeGreaterThan(revokeCalls)
  })

  it('aborts a pending old preview on job identity change and ignores its late blob', async () => {
    const alphaPreview = deferred()
    const betaPreview = deferred()
    const signals = []
    vi.spyOn(api, 'fetchPreview').mockImplementation((jobId, options) => {
      signals.push({ jobId, signal: options.signal })
      return jobId === 'job_alpha' ? alphaPreview.promise : betaPreview.promise
    })
    const view = renderWithSession(<VideoPreview jobId="job_alpha" previewAvailable />)
    await waitFor(() => expect(signals.some(({ jobId }) => jobId === 'job_alpha')).toBe(true))

    view.rerender(
      <AppProvider loadHealth={false}>
        <VideoPreview jobId="job_beta" previewAvailable />
      </AppProvider>,
    )

    expect(signals.find(({ jobId }) => jobId === 'job_alpha').signal.aborted).toBe(true)
    const objectUrlCalls = URL.createObjectURL.mock.calls.length
    alphaPreview.resolve(new Blob(['late-alpha-preview']))
    await act(async () => {})
    expect(URL.createObjectURL).toHaveBeenCalledTimes(objectUrlCalls)
    expect(screen.queryByRole('img', { name: /latest graph preview/i })).not.toBeInTheDocument()

    betaPreview.resolve(new Blob(['beta-preview']))
    expect(await screen.findByRole('img', { name: /latest graph preview/i })).toHaveAttribute('src', expect.stringMatching(/^blob:/))
    expect(document.body.innerHTML).not.toContain('late-alpha-preview')
  })
})
