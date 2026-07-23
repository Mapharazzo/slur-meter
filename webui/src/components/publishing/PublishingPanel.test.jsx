import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import PublishingPanel from './PublishingPanel'

const releases = [
  { id: 1, job_id: 'job_alpha', platform: 'youtube', remote_id: 'yt_123', status: 'uploaded', uploaded_at: '2026-07-23T10:00:00Z', safe_error: null, metadata: {}, updated_at: '2026-07-23T10:00:00Z' },
  { id: 2, job_id: 'job_alpha', platform: 'tiktok', remote_id: null, status: 'failed', uploaded_at: null, safe_error: { code: 'timeout', message: 'Upload timed out before submission.' }, metadata: {}, updated_at: '2026-07-23T10:01:00Z' },
  { id: 3, job_id: 'job_alpha', platform: 'instagram', remote_id: null, status: 'needs_attention', uploaded_at: null, safe_error: { code: 'ambiguous', message: 'The remote outcome is unknown.' }, metadata: {}, updated_at: '2026-07-23T10:02:00Z' },
]

const attempts = [
  { id: 10, job_id: 'job_alpha', platform: 'tiktok', retry_cycle: 1, attempt_number: 3, max_attempts: 3, trigger: 'automatic', started_at: '2026-07-23T09:58:00Z', finished_at: '2026-07-23T09:59:00Z', outcome: 'failed', retryable: true, safe_error: { code: 'timeout', message: 'Transient provider timeout.' }, remote_id: null, metadata: {} },
  { id: 11, job_id: 'job_alpha', platform: 'instagram', retry_cycle: 2, attempt_number: 1, max_attempts: 1, trigger: 'manual_retry', started_at: '2026-07-23T10:01:00Z', finished_at: '2026-07-23T10:02:00Z', outcome: 'ambiguous', retryable: false, safe_error: { code: 'ambiguous', message: 'Confirmation was not received.' }, remote_id: null, metadata: {} },
]

function props(overrides = {}) {
  return {
    jobId: 'job_alpha', token: 'session-token', releases, publishingAttempts: attempts,
    availableActions: ['retry_publish:tiktok', 'reconcile_publish:instagram', 'refresh_stats'],
    client: {
      publish: vi.fn().mockResolvedValue({ changed: true }),
      retryPublish: vi.fn().mockResolvedValue({ changed: true }),
      reconcilePublish: vi.fn().mockResolvedValue({ changed: true }),
      refreshStats: vi.fn().mockResolvedValue({ changed: true }),
    },
    onRefresh: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  }
}

describe('PublishingPanel', () => {
  it('renders durable platform releases and every associated attempt without publishing on render', () => {
    const options = props()
    render(<PublishingPanel {...options} />)

    expect(screen.getByRole('heading', { name: /^publishing$/i })).toBeInTheDocument()
    expect(screen.getByText('yt_123')).toBeInTheDocument()
    expect(screen.getByText(/attempt 3 of 3/i)).toBeInTheDocument()
    expect(screen.getByText(/cycle 1/i)).toBeInTheDocument()
    expect(screen.getByText(/automatic/i)).toBeInTheDocument()
    expect(screen.getByText(/transient provider timeout/i)).toBeInTheDocument()
    expect(screen.getByText(/failed · retryable/i)).toBeInTheDocument()
    expect(screen.getByText('2026-07-23T09:58:00Z')).toBeInTheDocument()
    expect(screen.getByText('2026-07-23T09:59:00Z')).toBeInTheDocument()
    expect(options.client.publish).not.toHaveBeenCalled()
    expect(options.client.retryPublish).not.toHaveBeenCalled()
    expect(options.client.reconcilePublish).not.toHaveBeenCalled()
  })

  it('marks uploaded releases complete, exposes only advertised controls, and submits explicit publish/retry with duplicate suppression', async () => {
    let resolveRetry
    const options = props({
      availableActions: ['publish:youtube', 'retry_publish:tiktok'],
    })
    options.client.retryPublish = vi.fn(() => new Promise((resolve) => { resolveRetry = resolve }))
    const user = userEvent.setup()
    render(<PublishingPanel {...options} />)

    const youtube = screen.getByRole('region', { name: /youtube publishing/i })
    expect(within(youtube).getByText(/uploaded complete/i)).toBeInTheDocument()
    expect(within(youtube).queryByRole('button', { name: /publish youtube/i })).not.toBeInTheDocument()

    const retry = screen.getByRole('button', { name: /retry tiktok publishing/i })
    await user.click(retry)
    expect(retry).toBeDisabled()
    await user.click(retry)
    expect(options.client.retryPublish).toHaveBeenCalledTimes(1)
    expect(options.client.retryPublish).toHaveBeenCalledWith('job_alpha', 'tiktok', { token: 'session-token', idempotencyKey: expect.stringMatching(/^ui_/) })
    resolveRetry({ changed: true })
    await waitFor(() => expect(options.onRefresh).toHaveBeenCalledTimes(1))

    const emptyOptions = props({ releases: [], publishingAttempts: [], availableActions: ['publish:youtube'] })
    render(<PublishingPanel {...emptyOptions} />)
    await user.click(screen.getByRole('button', { name: /publish youtube/i }))
    expect(emptyOptions.client.publish).toHaveBeenCalledWith('job_alpha', 'youtube', { token: 'session-token', idempotencyKey: expect.stringMatching(/^ui_/) })
  })

  it('keeps ambiguous outcomes persistent and validates explicit uploaded/not-uploaded reconciliation bodies', async () => {
    const options = props()
    const user = userEvent.setup()
    render(<PublishingPanel {...options} />)

    const instagram = screen.getByRole('region', { name: /^instagram publishing$/i })
    expect(within(instagram).getByRole('alert')).toHaveTextContent(/remote outcome is unknown/i)
    await user.click(within(instagram).getByRole('button', { name: /confirm uploaded/i }))
    expect(screen.getByText(/enter the remote id/i)).toBeInTheDocument()

    await user.type(within(instagram).getByLabelText(/remote id/i), 'bad remote id')
    await user.click(within(instagram).getByRole('button', { name: /confirm uploaded/i }))
    expect(screen.getByText(/supported characters/i)).toBeInTheDocument()
    expect(options.client.reconcilePublish).not.toHaveBeenCalled()

    await user.clear(within(instagram).getByLabelText(/remote id/i))
    await user.type(within(instagram).getByLabelText(/remote id/i), 'ig_456')
    await user.click(within(instagram).getByRole('button', { name: /confirm uploaded/i }))
    expect(options.client.reconcilePublish).toHaveBeenCalledWith(
      'job_alpha', 'instagram', { reconciliation: 'uploaded', remote_id: 'ig_456' },
      { token: 'session-token', idempotencyKey: expect.stringMatching(/^ui_/) },
    )

    await user.click(within(instagram).getByRole('button', { name: /confirm not uploaded/i }))
    expect(options.client.reconcilePublish).toHaveBeenLastCalledWith(
      'job_alpha', 'instagram', { reconciliation: 'not_uploaded' },
      { token: 'session-token', idempotencyKey: expect.stringMatching(/^ui_/) },
    )
  })

  it.each([
    [{ ...releases[2] }, /remote outcome is unknown/i],
    [{ ...releases[0], remote_id: null }, /unknown.*attention/i],
  ])('keeps incomplete durable publishing truth visible without an advertised reconciliation control', (release, expected) => {
    render(<PublishingPanel {...props({ releases: [release], publishingAttempts: [], availableActions: [] })} />)
    expect(screen.getByRole('alert')).toHaveTextContent(expected)
    expect(screen.queryByRole('button', { name: /confirm uploaded/i })).not.toBeInTheDocument()
  })

  it('surfaces publishing mutation failures inline while retaining durable release truth', async () => {
    const options = props()
    options.client.retryPublish = vi.fn().mockRejectedValue(new Error('Retry was rejected safely.'))
    const user = userEvent.setup()
    render(<PublishingPanel {...options} />)

    await user.click(screen.getByRole('button', { name: /retry tiktok publishing/i }))
    expect(await screen.findByText(/retry was rejected safely/i)).toBeInTheDocument()
    expect(screen.getByText('yt_123')).toBeInTheDocument()
  })
})
