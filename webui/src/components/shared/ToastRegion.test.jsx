import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import axe from 'axe-core'
import { describe, expect, it, vi } from 'vitest'

import ToastRegion from './ToastRegion'

describe('ToastRegion', () => {
  it('announces normal and error feedback with labelled dismiss buttons', async () => {
    const dismiss = vi.fn()
    const user = userEvent.setup()
    const { container } = render(<ToastRegion toasts={[
      { id: '1', type: 'success', message: 'Saved <safely>' },
      { id: '2', type: 'error', message: 'Could not save' },
    ]} onDismiss={dismiss} />)

    expect(screen.getByRole('status')).toHaveAttribute('aria-live', 'polite')
    expect(screen.getByRole('alert')).toHaveTextContent('Could not save')
    expect(screen.getByText('Saved <safely>')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Dismiss: Could not save' }))
    expect(dismiss).toHaveBeenCalledWith('2')
    expect((await axe.run(container)).violations).toEqual([])
  })
})
