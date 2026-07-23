import { useApp } from '../../context/AppContext'

function statusMessage(connectivity, health) {
  if (health?.status === 'stale' || connectivity?.status === 'stale') {
    return { kind: 'stale', text: 'System status stale — showing the last health check.' }
  }
  switch (connectivity?.status) {
    case 'ready':
      return { kind: 'ready', text: 'System ready — API connected and dispatcher available.' }
    case 'dispatcher_unavailable':
      return { kind: 'warning', text: 'Dispatcher unavailable — reads are online but queued work cannot start.' }
    case 'disconnected':
      return { kind: 'disconnected', text: 'Disconnected — the API health check could not be reached.' }
    case 'error':
      return { kind: 'error', text: 'System status error — health could not be verified.' }
    default:
      return { kind: 'checking', text: 'Checking system status…' }
  }
}

function StatusView({ connectivity, health }) {
  const status = statusMessage(connectivity, health)
  return (
    <div role="status" aria-live="polite" className={`system-status system-status--${status.kind}`}>
      <span className="system-status__mark" aria-hidden="true" />
      <span>{status.text}</span>
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
