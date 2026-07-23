import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import EventLog from './EventLog'

const events = [
  { id: 1, severity: 'info', message: 'Job claimed.', created_at: '2026-07-23T10:00:00Z' },
  { id: 2, severity: 'debug', message: 'Heartbeat renewed.', created_at: '2026-07-23T10:00:01Z' },
  { id: 3, severity: 'warning', message: 'Preview delayed.', created_at: '2026-07-23T10:00:02Z' },
]

describe('EventLog', () => {
  it('collapses, shows severities, and hides debug until toggled', async () => {
    const user = userEvent.setup()
    render(<EventLog events={events} />)

    const details = screen.getByText('Operational events').closest('details')
    expect(details).not.toHaveAttribute('open')
    expect(within(details).getByText(/3 total/)).toHaveTextContent(/1 debug/)

    // Non-debug events show with their level; the debug event is hidden.
    expect(screen.getByText('Job claimed.')).toBeInTheDocument()
    expect(screen.getByText('warning')).toBeInTheDocument()
    expect(screen.queryByText('Heartbeat renewed.')).toBeNull()

    await user.click(screen.getByLabelText(/show 1 debug event/i))
    expect(screen.getByText('Heartbeat renewed.')).toBeInTheDocument()
    expect(screen.getByText('debug')).toBeInTheDocument()
  })
})
