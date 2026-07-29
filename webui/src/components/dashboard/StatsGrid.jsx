const PRIMARY_STATES = ['running', 'needs_attention', 'failed', 'queued', 'completed']

function labelForState(state) {
  return state.replaceAll('_', ' ')
}

// Six separate counters said how many runs were in each state but never how the
// queue was shaped. One proportional bar answers that at a glance, and reuses
// the same track form as a run's pipeline timeline.
export default function StatsGrid({ summary }) {
  const total = Number.isFinite(summary?.total) ? summary.total : 0
  const states = summary?.states && typeof summary.states === 'object' ? summary.states : {}
  const orderedStates = [
    ...PRIMARY_STATES,
    ...Object.keys(states).filter((state) => !PRIMARY_STATES.includes(state)).sort(),
  ]
  const countFor = (state) => (Number.isFinite(states[state]) ? states[state] : 0)
  const occupied = orderedStates.filter((state) => countFor(state) > 0)

  return (
    <section aria-label="Operations summary" className="panel">
      <div className="panel__body">
        <div className="summary__total">
          <span className="summary__value">{total}</span>
          <span className="summary__label">total runs</span>
        </div>

        {occupied.length > 0 && (
          <div className="track" aria-hidden="true">
            {occupied.map((state) => (
              <span
                key={state}
                className="track__seg"
                data-state={state}
                style={{ flex: countFor(state) }}
              />
            ))}
          </div>
        )}

        <dl className="summary__states">
          {orderedStates.map((state) => (
            <div key={state} className={`summary-state summary-state--${state}`}>
              <dt>{labelForState(state)}</dt>
              <dd>{countFor(state)}</dd>
            </div>
          ))}
        </dl>
      </div>
    </section>
  )
}
