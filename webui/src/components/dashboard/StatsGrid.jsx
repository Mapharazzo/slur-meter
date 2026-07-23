const PRIMARY_STATES = ['running', 'needs_attention', 'failed', 'queued', 'completed']

function labelForState(state) {
  return state.replaceAll('_', ' ')
}

export default function StatsGrid({ summary }) {
  const total = Number.isFinite(summary?.total) ? summary.total : 0
  const states = summary?.states && typeof summary.states === 'object' ? summary.states : {}
  const orderedStates = [
    ...PRIMARY_STATES,
    ...Object.keys(states).filter((state) => !PRIMARY_STATES.includes(state)).sort(),
  ]

  return (
    <section aria-label="Operations summary" className="summary-panel">
      <div className="summary-total">
        <span className="summary-value">{total}</span>
        <span className="summary-label">total runs</span>
      </div>
      <dl className="summary-states">
        {orderedStates.map((state) => (
          <div key={state} className={`summary-state summary-state--${state}`}>
            <dt>{labelForState(state)}</dt>
            <dd>{Number.isFinite(states[state]) ? states[state] : 0}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
