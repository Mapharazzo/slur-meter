import { Link } from 'react-router-dom'

const GROUPS = [
  { key: 'active', label: 'Active', states: ['running'], description: 'Work currently owned by the dispatcher.' },
  { key: 'attention', label: 'Needs attention', states: ['needs_attention'], description: 'Runs waiting for an operator decision.' },
  { key: 'failed', label: 'Failed', states: ['failed'], description: 'Runs that exhausted automatic recovery.' },
  { key: 'queued', label: 'Queued', states: ['queued'], description: 'Durable work waiting for a dispatcher.' },
  { key: 'completed', label: 'Recently completed', states: ['completed'], description: 'Recently finished runs.' },
]

function text(value) {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

function stateLabel(state) {
  return String(state || 'unknown').replaceAll('_', ' ')
}

function formatDate(value) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'Update time unavailable' : `Updated ${date.toLocaleString()}`
}

function RunRow({ run }) {
  const safeError = text(run.safe_error?.message)
  const nextAction = text(run.next_action)
  return (
    <li className={`queue-row queue-row--${run.state}`}>
      <Link to={`/jobs/${encodeURIComponent(run.id)}`} className="queue-row__link">
        <span className="queue-row__identity">
          <strong>{text(run.label) || run.id}</strong>
          <span>{run.source_imdb_id || run.id}</span>
        </span>
        <span className="queue-row__state">
          <span className="state-mark" aria-hidden="true" />
          {stateLabel(run.state)}
        </span>
        <span className="queue-row__stage">{text(run.current_stage)?.replaceAll('_', ' ') || 'No active stage'}</span>
        <span className="queue-row__detail">
          {safeError && <span><strong>Stopped:</strong> {safeError}</span>}
          {nextAction && <span><strong>Next:</strong> {nextAction}</span>}
          {!safeError && !nextAction && <span>No operator action required.</span>}
        </span>
        <time dateTime={run.updated_at}>{formatDate(run.updated_at)}</time>
      </Link>
    </li>
  )
}

export default function JobList({ jobs = [] }) {
  const knownStates = new Set(GROUPS.flatMap((group) => group.states))
  const groups = [
    ...GROUPS,
    ...(
      jobs.some((run) => !knownStates.has(run.state))
        ? [{ key: 'other', label: 'Other operational states', states: [], description: 'Runs using a state added by a newer service.' }]
        : []
    ),
  ]

  return (
    <div className="queue-groups">
      {groups.map((group) => {
        const rows = group.key === 'other'
          ? jobs.filter((run) => !knownStates.has(run.state))
          : jobs.filter((run) => group.states.includes(run.state))
        return (
          <section key={group.key} className={`queue-group queue-group--${group.key}`} aria-labelledby={`queue-${group.key}`}>
            <div className="queue-group__heading">
              <div>
                <h2 id={`queue-${group.key}`}>{group.label}</h2>
                <p>{group.description}</p>
              </div>
              <span className="queue-group__count" aria-label={`${rows.length} shown`}>{rows.length}</span>
            </div>
            {rows.length > 0 ? (
              <ul className="queue-list">
                {rows.map((run) => <RunRow key={run.id} run={run} />)}
              </ul>
            ) : (
              <p className="queue-group__empty">No runs in this group.</p>
            )}
          </section>
        )
      })}
    </div>
  )
}
