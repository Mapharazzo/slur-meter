import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => {
  cleanup()
  try {
    window.sessionStorage.clear()
    window.localStorage.clear()
  } catch {
    // Individual storage-failure tests restore their spies during Vitest cleanup.
  }
})
