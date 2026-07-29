function title(state) {
  if (state === 'needs_attention') return 'Operator attention required'
  if (state === 'failed') return 'Run failed'
  if (state === 'cancelled') return 'Run cancelled'
  return 'Run stopped'
}

export default function AttentionBanner({ run, availableActions = [], pendingAction, onAction }) {
  if (!['needs_attention', 'failed', 'cancelled'].includes(run?.state)) return null
  return (
    <section role="alert" className="notice notice--warning">
      <h2>{title(run.state)}</h2>
      <p>{run.safe_error?.message || 'Automatic processing has stopped.'}</p>
      {run.next_action && <p><strong>Next action:</strong> {run.next_action}</p>}
      <div className="notice__actions">
        {availableActions.includes('resume') && <button type="button" className="button button--primary" disabled={Boolean(pendingAction)} onClick={() => onAction('resume')}>Resume run</button>}
        {availableActions.includes('cancel') && <button type="button" className="button" disabled={Boolean(pendingAction)} onClick={() => onAction('cancel')}>Cancel run</button>}
      </div>
    </section>
  )
}
