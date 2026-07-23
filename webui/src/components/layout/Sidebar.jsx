import { useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'

const NAVIGATION = [
  { path: '/', label: 'Operations queue', mark: 'O' },
  { path: '/leaderboard', label: 'Leaderboard', mark: 'L' },
  { path: '/costs', label: 'Costs', mark: 'C' },
  { path: '/revenue', label: 'Revenue', mark: 'R' },
  { path: '/alerts', label: 'Alerts', mark: 'A' },
]

function isCurrent(pathname, path) {
  if (path === '/') return pathname === '/' || pathname === '/jobs' || pathname.startsWith('/jobs/')
  return pathname === path || pathname.startsWith(`${path}/`)
}

function desktopNavigation() {
  if (typeof window === 'undefined') return true
  if (typeof window.matchMedia === 'function') return window.matchMedia('(min-width: 960px)').matches
  return window.innerWidth >= 960
}

export default function Sidebar({ open: initiallyOpen = false, onClose }) {
  const [open, setOpen] = useState(initiallyOpen)
  const [isDesktop, setIsDesktop] = useState(desktopNavigation)
  const location = useLocation()

  const close = () => {
    setOpen(false)
    onClose?.()
  }

  useEffect(() => {
    if (!open) return undefined
    const handleKeyDown = (event) => {
      if (event.key === 'Escape') close()
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [open])

  useEffect(() => {
    const media = typeof window.matchMedia === 'function' ? window.matchMedia('(min-width: 960px)') : null
    const update = () => setIsDesktop(media ? media.matches : desktopNavigation())
    if (media) media.addEventListener('change', update)
    else window.addEventListener('resize', update)
    return () => {
      if (media) media.removeEventListener('change', update)
      else window.removeEventListener('resize', update)
    }
  }, [])

  return (
    <>
      <button
        type="button"
        className="navigation-toggle"
        aria-controls="primary-navigation"
        aria-expanded={open}
        aria-label={open ? 'Close navigation' : 'Open navigation'}
        onClick={() => setOpen((current) => !current)}
      >
        <span aria-hidden="true">{open ? '×' : '☰'}</span>
      </button>
      {open && <button type="button" className="navigation-scrim" aria-label="Dismiss navigation backdrop" onClick={close} />}
      <aside className="sidebar" data-open={open ? 'true' : 'false'} hidden={!isDesktop && !open}>
        <Link to="/" className="brand" onClick={close}>
          <span className="brand__mark" aria-hidden="true">SM</span>
          <span>
            <strong>Slur Meter</strong>
            <small>Operations control</small>
          </span>
        </Link>
        <nav id="primary-navigation" aria-label="Primary" className="primary-navigation">
          <ul>
            {NAVIGATION.map((item) => {
              const current = isCurrent(location.pathname, item.path)
              return (
                <li key={item.path}>
                  <Link to={item.path} aria-current={current ? 'page' : undefined} onClick={close}>
                    <span className="nav-mark" aria-hidden="true">{item.mark}</span>
                    {item.label}
                  </Link>
                </li>
              )
            })}
          </ul>
        </nav>
        <p className="sidebar__footer">Single-dispatcher operations</p>
      </aside>
    </>
  )
}
