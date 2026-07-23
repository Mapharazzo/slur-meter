import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import SubtitleCandidates from './SubtitleCandidates'

const candidates = [1, 2, 3].map((rank) => ({
  id: `cand_${rank}`, job_id: 'job_alpha', provider: 'opensubtitles', provider_id: `remote_${rank}`,
  provider_filename: `candidate-${rank}.srt`, source_type: 'provider', language: 'en', fps: 24,
  title: 'Pulp Fiction', year: 1994, imdb_match: rank === 1, provider_rating: 8.2,
  provider_download_count: 1000 - rank, discovery_cycle: 1, rank, detected_encoding: 'utf-8',
  cue_count: 100, first_cue_seconds: 1, final_cue_seconds: 6000,
  parsed_duration_seconds: 6000, expected_runtime_seconds: 9000,
  coverage_percent: rank === 1 ? 69.9 : (rank === 2 ? 70 : 125), download_error: null,
  parse_error: null, status: 'rejected', selected_at: null, selection_method: null,
  created_at: '2026-07-23T10:00:00Z', updated_at: '2026-07-23T10:00:00Z',
  rank_reasons: ['Exact title match'], quality_reasons: rank === 3 ? ['Coverage exceeds 120%.'] : [],
  rejection_reasons: [`Candidate ${rank} failed quality validation.`], artifact_available: true,
}))

function props(overrides = {}) {
  return {
    jobId: 'job_alpha', token: 'session-token', candidates,
    availableActions: ['select_subtitle:cand_1', 'rediscover_subtitles', 'resume'],
    client: {
      selectSubtitle: vi.fn().mockResolvedValue({ changed: true }),
      rediscoverSubtitles: vi.fn().mockResolvedValue({ changed: true }),
      uploadSubtitle: vi.fn().mockResolvedValue({ candidate: { id: 'cand_upload' } }),
      resumeJob: vi.fn().mockResolvedValue({ changed: true }),
    },
    onRefresh: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  }
}

describe('SubtitleCandidates', () => {
  it('shows every rejected candidate with comparison truth and the inclusive 70 percent threshold', () => {
    const { container } = render(<SubtitleCandidates {...props()} />)
    expect(screen.getByText(/acceptance threshold: 70%/i)).toBeInTheDocument()
    expect(screen.getAllByText(/rejected/i)).toHaveLength(3)
    expect(screen.getByText('69.9%')).toBeInTheDocument()
    expect(screen.getByText('70%')).toBeInTheDocument()
    expect(screen.getByText('125%')).toBeInTheDocument()
    expect(screen.getByText(/coverage exceeds 120/i)).toBeInTheDocument()
    expect(screen.getByText(/candidate 1 failed quality/i)).toBeInTheDocument()
    expect(container).toHaveTextContent(/parsed duration/i)
    expect(container).toHaveTextContent(/expected duration/i)
    expect(container).toHaveTextContent(/selection method/i)
    expect(screen.getAllByRole('columnheader').every((header) => header.getAttribute('scope') === 'col')).toBe(true)
  })

  it('exposes selection only for the matching action and uses one explicit token/idempotency attempt while suppressing duplicates', async () => {
    let resolveSelection
    const options = props()
    options.client.selectSubtitle = vi.fn(() => new Promise((resolve) => { resolveSelection = resolve }))
    const user = userEvent.setup()
    render(<SubtitleCandidates {...options} />)

    const row = screen.getByRole('row', { name: /candidate-1\.srt/i })
    const select = within(row).getByRole('button', { name: /select candidate 1/i })
    expect(screen.queryByRole('button', { name: /select candidate 2/i })).not.toBeInTheDocument()
    await user.click(select)
    expect(select).toBeDisabled()
    await user.click(select)
    expect(options.client.selectSubtitle).toHaveBeenCalledTimes(1)
    expect(options.client.selectSubtitle).toHaveBeenCalledWith('job_alpha', 'cand_1', { token: 'session-token', idempotencyKey: expect.stringMatching(/^ui_/) })
    resolveSelection({ changed: true })
    await waitFor(() => expect(options.onRefresh).toHaveBeenCalledTimes(1))
  })

  it('supports rediscovery, validated SRT upload, and resume as separate deliberate mutations', async () => {
    const options = props({ availableActions: ['rediscover_subtitles', 'resume'] })
    const user = userEvent.setup()
    const { rerender } = render(<SubtitleCandidates {...options} />)

    await user.click(screen.getByRole('button', { name: /rediscover subtitles/i }))
    expect(options.client.rediscoverSubtitles).toHaveBeenCalledWith('job_alpha', { token: 'session-token', idempotencyKey: expect.stringMatching(/^ui_/) })
    await waitFor(() => expect(options.onRefresh).toHaveBeenCalled())

    const file = new File(['1\n00:00:01,000 --> 00:00:02,000\nHello'], 'manual.srt', { type: 'application/x-subrip' })
    await user.upload(screen.getByLabelText(/upload srt/i), file)
    await user.click(screen.getByRole('button', { name: /upload subtitle/i }))
    expect(options.client.uploadSubtitle).toHaveBeenCalledWith('job_alpha', file, { token: 'session-token', idempotencyKey: expect.stringMatching(/^ui_/) })

    await user.click(screen.getByRole('button', { name: /^resume run$/i }))
    expect(options.client.resumeJob).toHaveBeenCalledWith('job_alpha', { token: 'session-token', idempotencyKey: expect.stringMatching(/^ui_/) })

    rerender(<SubtitleCandidates {...options} />)
    await user.upload(screen.getByLabelText(/upload srt/i), new File(['x'], 'notes.txt'))
    await user.click(screen.getByRole('button', { name: /upload subtitle/i }))
    expect(screen.getByRole('alert')).toHaveTextContent(/choose an \.srt file/i)
  })

  it('keeps mutation failures inline and durable candidate rows visible', async () => {
    const options = props()
    options.client.rediscoverSubtitles = vi.fn().mockRejectedValue(new Error('Rediscovery was rejected safely.'))
    const user = userEvent.setup()
    render(<SubtitleCandidates {...options} />)

    await user.click(screen.getByRole('button', { name: /rediscover subtitles/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/rediscovery was rejected safely/i)
    expect(screen.getByRole('row', { name: /candidate-1\.srt/i })).toBeInTheDocument()
  })
})
