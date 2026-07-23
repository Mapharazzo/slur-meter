import { useState } from 'react'

const LEVEL_STYLES = {
  debug: 'text-gray-400',
  info: 'text-sky-400',
  warning: 'text-amber-400',
  error: 'text-red-400',
  critical: 'text-red-500',
}

function levelOf(event) {
  return String(event.severity || 'info').toLowerCase()
}

export default function EventLog({ events = [], warning = '' }) {
  const [showDebug, setShowDebug] = useState(false)
  const debugCount = events.filter((event) => levelOf(event) === 'debug').length
  const visible = showDebug ? events : events.filter((event) => levelOf(event) !== 'debug')

  return (
    <details className="glass rounded-2xl p-5">
      <summary className="flex cursor-pointer flex-wrap items-center gap-2 font-semibold">
        <span>Operational events</span>
        <span className="text-sm font-normal opacity-70">
          · {events.length} total{debugCount ? ` · ${debugCount} debug` : ''} · expand
        </span>
      </summary>
      <div className="mt-4 space-y-3">
        {warning && <p role="alert">{warning} Showing persisted events.</p>}
        {debugCount > 0 && (
          <label className="flex items-center gap-2 text-sm opacity-80">
            <input type="checkbox" checked={showDebug} onChange={(event) => setShowDebug(event.target.checked)} />
            Show {debugCount} debug event{debugCount === 1 ? '' : 's'}
          </label>
        )}
        {visible.length ? (
          <ol className="space-y-1 text-sm">
            {visible.map((event) => {
              const level = levelOf(event)
              return (
                <li key={event.id} className="flex flex-wrap items-baseline gap-2">
                  <span className={`w-16 shrink-0 font-mono text-xs uppercase ${LEVEL_STYLES[level] || LEVEL_STYLES.info}`}>{level}</span>
                  <time dateTime={event.created_at} className="opacity-60">{event.created_at}</time>
                  <span>{event.message}</span>
                </li>
              )
            })}
          </ol>
        ) : <p>No events to show{debugCount ? ' (debug hidden).' : '.'}</p>}
      </div>
    </details>
  )
}
