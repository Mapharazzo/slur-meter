// Colour follows the shared state palette in index.css: tungsten for work in
// progress, daylight for finished, sun for a hold, tally for a stop. The state
// name is always spelled out, so colour is never the only signal.
export default function StatusBadge({ status }) {
  const state = String(status || 'pending')
  return (
    <span className={`badge badge--${state}`}>
      {state.replaceAll('_', ' ')}
    </span>
  )
}
