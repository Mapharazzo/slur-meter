import { useState } from 'react'

// Severity colour is driven by [data-level] in the design system, so the log
// stays on the same palette as every other state signal in the panel.
function levelOf(event) {
  return String(event.severity || 'info').toLowerCase()
}

export default function EventLog({ events = [], warning = '' }) {
  const [showDebug, setShowDebug] = useState(false)
  const debugCount = events.filter((event) => levelOf(event) === 'debug').length
  const visible = showDebug ? events : events.filter((event) => levelOf(event) !== 'debug')

  return (
    <details className="disclosure">
      <summary>
        <span>Operational events</span>
        <span className="disclosure__note">
          {events.length} total{debugCount ? ` · ${debugCount} debug` : ''} · expand
        </span>
      </summary>
      <div className="disclosure__body">
        {warning && <p role="alert" className="notice notice--warning">{warning} Showing persisted events.</p>}
        {debugCount > 0 && (
          <label className="checkbox-row">
            <input type="checkbox" checked={showDebug} onChange={(event) => setShowDebug(event.target.checked)} />
            Show {debugCount} debug event{debugCount === 1 ? '' : 's'}
          </label>
        )}
        {visible.length ? (
          <ol className="event-list">
            {visible.map((event) => {
              const level = levelOf(event)
              return (
                <li key={event.id} className="event-row">
                  <span className="event-row__level" data-level={level}>{level}</span>
                  <time dateTime={event.created_at}>{event.created_at}</time>
                  <span>{event.message}</span>
                </li>
              )
            })}
          </ol>
        ) : <p className="hint">No events to show{debugCount ? ' (debug hidden).' : '.'}</p>}
      </div>
    </details>
  )
}
