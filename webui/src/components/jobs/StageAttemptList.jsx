function duration(startedAt, finishedAt) {
  if (!startedAt || !finishedAt) return null
  const seconds = Math.max(0, Math.round((Date.parse(finishedAt) - Date.parse(startedAt)) / 1000))
  if (!Number.isFinite(seconds)) return null
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  if (!minutes) return `${remainder} second${remainder === 1 ? '' : 's'}`
  if (!remainder) return `${minutes} minute${minutes === 1 ? '' : 's'}`
  return `${minutes} minute${minutes === 1 ? '' : 's'} ${remainder} second${remainder === 1 ? '' : 's'}`
}

function label(value) {
  return String(value || 'unknown').replaceAll('_', ' ')
}

export default function StageAttemptList({ attempts = [] }) {
  if (!attempts.length) return <p>No persisted attempts.</p>
  return (
    <section aria-label="Attempt history">
      <h4>Attempt history</h4>
      <ol className="space-y-2">
        {attempts.map((attempt) => (
          <li key={attempt.id} className="rounded-lg border border-white/10 p-3 text-sm">
            <p>
              <strong>Cycle {attempt.retry_cycle}</strong>{' · '}
              Attempt {attempt.attempt_number} of {attempt.max_attempts}{' · '}
              {label(attempt.trigger)}
            </p>
            <p>{label(attempt.outcome)} · {attempt.retryable ? 'Retryable' : 'Not retryable'}</p>
            <p>
              <time dateTime={attempt.started_at}>{attempt.started_at}</time>
              {attempt.finished_at && <> — <time dateTime={attempt.finished_at}>{attempt.finished_at}</time></>}
              {duration(attempt.started_at, attempt.finished_at) && <> · {duration(attempt.started_at, attempt.finished_at)}</>}
            </p>
          </li>
        ))}
      </ol>
    </section>
  )
}
