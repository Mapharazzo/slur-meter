import { useApp } from '../../context/AppContext'

function statusMessage(connectivity, health) {
  if (health?.status === 'stale' || connectivity?.status === 'stale') {
    return { kind: 'stale', short: 'Status stale', text: 'System status stale — showing the last health check.' }
  }
  switch (connectivity?.status) {
    case 'ready':
      return { kind: 'ready', short: 'Ready', text: 'System ready — API connected and dispatcher available.' }
    case 'dispatcher_unavailable':
      return { kind: 'warning', short: 'Dispatcher down', text: 'Dispatcher unavailable — reads are online but queued work cannot start.' }
    case 'disconnected':
      return { kind: 'disconnected', short: 'Disconnected', text: 'Disconnected — the API health check could not be reached.' }
    case 'error':
      return { kind: 'error', short: 'Status error', text: 'System status error — health could not be verified.' }
    default:
      return { kind: 'checking', short: 'Checking…', text: 'Checking system status…' }
  }
}

// The chip shows a short reading; the full sentence stays in the accessible
// name and the tooltip, so a screen reader still hears why the system is in
// this state and a hover explains it.
function StatusView({ connectivity, health }) {
  const status = statusMessage(connectivity, health)
  return (
    <div
      role="status"
      aria-live="polite"
      title={status.text}
      className={`status-chip status-chip--${status.kind}`}
    >
      <span className="status-chip__mark" aria-hidden="true" />
      <span className="sr-only">{status.text}</span>
      <span aria-hidden="true">{status.short}</span>
    </div>
  )
}

function ConnectedStatus() {
  const { connectivity, health } = useApp()
  return <StatusView connectivity={connectivity} health={health} />
}

export default function SystemStatusBar(props) {
  if (props.connectivity || props.health) return <StatusView {...props} />
  return <ConnectedStatus />
}
