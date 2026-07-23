import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import StageTimeline from './StageTimeline'

const base = {
  retry_cycle: 1, max_auto_attempts: 3, started_at: null, finished_at: null,
  warnings: [], output_manifest: {}, safe_error: null, next_action: null,
}

function renderTimeline(stages) {
  render(
    <StageTimeline
      stages={stages}
      attempts={[]}
      availableActions={[]}
      pendingAction={null}
      onRetry={() => {}}
    />,
  )
}

describe('StageTimeline composite child display', () => {
  it('shows a fully-rendered running child as completed, not a stale spinner', () => {
    const composite = { ...base, id: 20, name: 'composite', parent_stage_id: null, ordinal: 7, state: 'running', progress: { numerator: 60, denominator: 240, unit: 'frames' } }
    // Finished its frames but still `running` pending the atomic parent commit.
    const introHold = { ...base, id: 21, name: 'intro_hold', parent_stage_id: 20, ordinal: 2, state: 'running', progress: { numerator: 60, denominator: 60, unit: 'frames' } }
    // Actively rendering.
    const graphChild = { ...base, id: 22, name: 'graph', parent_stage_id: 20, ordinal: 3, state: 'running', progress: { numerator: 14, denominator: 480, unit: 'frames' } }

    renderTimeline([composite, introHold, graphChild])

    const introHoldItem = screen.getByRole('heading', { name: 'intro hold' }).closest('li')
    expect(within(introHoldItem).getByText('completed')).toBeInTheDocument()
    expect(within(introHoldItem).queryByText('running')).toBeNull()

    const graphItem = screen.getByRole('heading', { name: 'graph' }).closest('li')
    expect(within(graphItem).getByText('running')).toBeInTheDocument()
  })

  it('does not relabel a top-level running stage that is at full progress', () => {
    // A parent/top-level stage doing genuine final work keeps `running`.
    const encode = { ...base, id: 30, name: 'encode', parent_stage_id: null, ordinal: 9, state: 'running', progress: { numerator: 100, denominator: 100, unit: 'frames' } }

    renderTimeline([encode])

    const encodeItem = screen.getByRole('heading', { name: 'encode' }).closest('li')
    expect(within(encodeItem).getByText('running')).toBeInTheDocument()
  })
})
