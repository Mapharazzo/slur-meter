import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import axe from 'axe-core'
import { describe, expect, it, vi } from 'vitest'

import ResourceState from './ResourceState'

describe('ResourceState', () => {
  it('never renders empty before a successful response', () => {
    render(<ResourceState resource={{ status: 'loading', data: undefined }}>content</ResourceState>)
    expect(screen.getByRole('status')).toHaveTextContent(/loading/i)
    expect(screen.queryByText(/no results/i)).not.toBeInTheDocument()
  })

  it('does not evaluate data renderers during initial loading', () => {
    render(
      <ResourceState resource={{ status: 'loading', data: undefined }}>
        {(data) => data.items.map((item) => <span key={item}>{item}</span>)}
      </ResourceState>,
    )

    expect(screen.getByRole('status')).toHaveTextContent(/loading/i)
  })

  it('renders successful empty and normal success honestly', () => {
    const { rerender } = render(<ResourceState resource={{ status: 'success', data: [] }} isEmpty={(data) => data.length === 0}>rows</ResourceState>)
    expect(screen.getByText(/no results/i)).toBeInTheDocument()
    rerender(<ResourceState resource={{ status: 'success', data: [1] }}>rows</ResourceState>)
    expect(screen.getByText('rows')).toBeInTheDocument()
  })

  it('shows errors with a real labelled retry control', async () => {
    const retry = vi.fn()
    const user = userEvent.setup()
    render(<ResourceState resource={{ status: 'error', error: new Error('Safe failure') }} onRetry={retry}>hidden</ResourceState>)
    expect(screen.getByRole('alert')).toHaveTextContent('Safe failure')
    await user.click(screen.getByRole('button', { name: /retry resource/i }))
    expect(retry).toHaveBeenCalledTimes(1)
  })

  it.each(['stale', 'disconnected'])('keeps cached content visible with a %s warning', async (status) => {
    const { container } = render(<ResourceState resource={{ status, data: { id: 1 }, error: new Error('Connection unavailable') }}>cached content</ResourceState>)
    expect(screen.getByText('cached content')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(status === 'stale' ? /stale/i : /connection/i)
    expect((await axe.run(container)).violations).toEqual([])
  })

  it('does not hide disconnected failure when there is no cached data', () => {
    render(<ResourceState resource={{ status: 'disconnected', data: undefined, error: new Error('Offline') }}>hidden</ResourceState>)
    expect(screen.getByRole('alert')).toHaveTextContent(/offline/i)
    expect(screen.queryByText('hidden')).not.toBeInTheDocument()
  })

  it('uses the polling resource refresh control when no retry override is supplied', async () => {
    const refresh = vi.fn()
    const user = userEvent.setup()
    render(<ResourceState resource={{ status: 'error', error: new Error('Failed'), refresh }}>hidden</ResourceState>)

    await user.click(screen.getByRole('button', { name: /retry resource/i }))

    expect(refresh).toHaveBeenCalledTimes(1)
  })
})
