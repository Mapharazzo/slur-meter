import { useMemo, useState } from 'react'

const BLOCKED_KEY = /token|authorization|cookie|header|path|body|secret|credential/i
const ABSOLUTE_PATH = /(?:^|\s)(?:\/[\w.-]+){2,}|[A-Za-z]:\\/

function sanitize(value, secret, depth = 0) {
  if (depth > 5) return '[bounded]'
  if (typeof value === 'string') {
    if (ABSOLUTE_PATH.test(value)) return '[redacted path]'
    const bounded = value.slice(0, 500)
    return secret ? bounded.split(secret).join('[redacted]') : bounded
  }
  if (Array.isArray(value)) return value.slice(0, 100).map((item) => sanitize(item, secret, depth + 1))
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).filter(([key]) => !BLOCKED_KEY.test(key)).slice(0, 100).map(([key, item]) => [
      secret ? key.split(secret).join('[redacted]') : key,
      sanitize(item, secret, depth + 1),
    ]))
  }
  return value
}

function publicDiagnostics(detail, token) {
  if (!detail) return {}
  return sanitize({
    run: detail.run,
    stages: detail.stages,
    attempts: detail.attempts,
    events: detail.events,
    decisions: detail.decisions,
    publishing_attempts: detail.publishing_attempts,
    releases: detail.releases,
    server_time: detail.server_time,
    last_event_id: detail.last_event_id,
    available_actions: detail.available_actions,
  }, typeof token === 'string' ? token.trim() : '')
}

export default function DiagnosticsPanel({ detail, operatorToken = '' }) {
  const [open, setOpen] = useState(false)
  const [feedback, setFeedback] = useState('')
  const diagnostics = useMemo(() => publicDiagnostics(detail, operatorToken), [detail, operatorToken])
  const json = JSON.stringify(diagnostics, null, 2)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(json)
      setFeedback('Diagnostics copied.')
    } catch {
      setFeedback('Diagnostics could not be copied.')
    }
  }
  return (
    <details className="glass rounded-xl p-4" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary onClick={(event) => { event.preventDefault(); setOpen((value) => !value) }}>Technical diagnostics</summary>
      {open && <>
        <pre className="mt-3 max-h-80 overflow-auto whitespace-pre-wrap text-xs">{json}</pre>
        <button type="button" className="button mt-3" onClick={copy}>Copy sanitized diagnostics</button>
        {feedback && <p role="status">{feedback}</p>}
      </>}
    </details>
  )
}
