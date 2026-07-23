import { useState } from 'react'

import StatusBadge from '../shared/StatusBadge'
import StageAttemptList from './StageAttemptList'

function words(value) {
  return String(value || '').replaceAll('.', ' ').replaceAll('_', ' ')
}

function duration(startedAt, finishedAt) {
  if (!startedAt) return null
  const end = finishedAt ? Date.parse(finishedAt) : Date.now()
  const seconds = Math.max(0, Math.round((end - Date.parse(startedAt)) / 1000))
  if (!Number.isFinite(seconds)) return null
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  if (!minutes) return `${rest} second${rest === 1 ? '' : 's'}`
  if (!rest) return `${minutes} minute${minutes === 1 ? '' : 's'}`
  return `${minutes} minute${minutes === 1 ? '' : 's'} ${rest} second${rest === 1 ? '' : 's'}`
}

function StageProgress({ stage }) {
  const { numerator, denominator, unit } = stage.progress || {}
  if (numerator == null || denominator == null || denominator <= 0) {
    return stage.state === 'running' ? <p role="status">Progress is indeterminate.</p> : null
  }
  return (
    <div>
      <progress
        aria-label={`${words(stage.name)} progress`}
        value={numerator}
        max={denominator}
        aria-valuemin="0"
        aria-valuenow={numerator}
        aria-valuemax={denominator}
        className="w-full"
      />
      <p>{numerator} of {denominator}{unit ? ` ${unit}` : ''}</p>
    </div>
  )
}

function isProgressComplete(stage) {
  const { numerator, denominator } = stage.progress || {}
  return denominator > 0 && numerator != null && numerator >= denominator
}

// A composite child stays `running` at full progress until its parent's
// artifact is durably promoted and every child completes atomically. Show such
// a fully-rendered child as completed so it doesn't read as a stale spinner.
function displayState(stage) {
  if (stage.parent_stage_id != null && stage.state === 'running' && isProgressComplete(stage)) {
    return 'completed'
  }
  return stage.state
}

function StageItem({ stage, attempts, children, canRetry, busy, onRetry }) {
  const [expanded, setExpanded] = useState(false)
  const headingId = `stage-${stage.id}`
  return (
    <li data-parent-stage={stage.parent_stage_id ?? undefined} className="rounded-xl border border-white/10 bg-white/[0.02] p-3">
      <div className="flex w-full items-center gap-3">
        <h3 id={headingId} className="flex-1 font-semibold capitalize">{words(stage.name)}</h3>
        <StatusBadge status={displayState(stage)} />
        <button
          type="button"
          aria-label={`${expanded ? 'Collapse' : 'Expand'} ${words(stage.name)}`}
          aria-expanded={expanded}
          aria-controls={`${headingId}-panel`}
          onClick={() => setExpanded((value) => !value)}
        >
          <span aria-hidden="true">{expanded ? '−' : '+'}</span>
        </button>
      </div>
      {children}
      {canRetry && !expanded && (
        <button type="button" className="button mt-3" disabled={busy} onClick={() => onRetry(stage)}>
          {busy ? `Retrying ${words(stage.name)}…` : `Retry ${words(stage.name)}`}
        </button>
      )}
      {expanded && (
        <div id={`${headingId}-panel`} className="mt-3 space-y-3 border-t border-white/10 pt-3">
          <StageProgress stage={stage} />
          <dl className="grid gap-2 text-sm sm:grid-cols-2">
            <div><dt>Retry cycle</dt><dd>Cycle {stage.retry_cycle}</dd></div>
            <div><dt>Automatic limit</dt><dd>{stage.max_auto_attempts}</dd></div>
            <div><dt>Started</dt><dd>{stage.started_at ? <time dateTime={stage.started_at}>{stage.started_at}</time> : 'Not started'}</dd></div>
            <div><dt>Finished / duration</dt><dd>{stage.finished_at ? <time dateTime={stage.finished_at}>{stage.finished_at}</time> : 'In progress'}{duration(stage.started_at, stage.finished_at) && <> · {duration(stage.started_at, stage.finished_at)}</>}</dd></div>
          </dl>
          {stage.warnings?.length > 0 && (
            <section aria-label={`${words(stage.name)} warnings`} className="rounded-lg border border-amber-400/30 p-3">
              <h4>Warnings</h4>
              <ul>{stage.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
            </section>
          )}
          {stage.safe_error?.message && <p role="alert">{stage.safe_error.message}</p>}
          {Object.keys(stage.output_manifest || {}).filter((key) => !/path|secret|token|header|body/i.test(key)).length > 0 && (
            <dl>
              <dt>Manifest output summary</dt>
              {Object.entries(stage.output_manifest).filter(([key]) => !/path|secret|token|header|body/i.test(key)).map(([key, value]) => (
                <dd key={key}>{words(key)}: {String(value)}</dd>
              ))}
            </dl>
          )}
          {stage.next_action && <p><strong>Next action:</strong> {stage.next_action}</p>}
          <StageAttemptList attempts={attempts} />
          {canRetry && (
            <button type="button" className="button button--primary" disabled={busy} onClick={() => onRetry(stage)}>
              {busy ? `Retrying ${words(stage.name)}…` : `Retry ${words(stage.name)}`}
            </button>
          )}
        </div>
      )}
    </li>
  )
}

export default function StageTimeline({ stages = [], attempts = [], availableActions = [], pendingAction, onRetry }) {
  const sorted = [...stages].sort((left, right) => left.ordinal - right.ordinal || left.id - right.id)
  const childrenByParent = new Map()
  sorted.filter((stage) => stage.parent_stage_id != null).forEach((stage) => {
    const current = childrenByParent.get(stage.parent_stage_id) || []
    current.push(stage)
    childrenByParent.set(stage.parent_stage_id, current)
  })
  const renderStage = (stage) => (
    <StageItem
      key={stage.id}
      stage={stage}
      attempts={attempts.filter((attempt) => attempt.stage_id === stage.id)}
      canRetry={availableActions.includes(`retry_stage:${stage.name}`)}
      busy={pendingAction === `retry_stage:${stage.name}`}
      onRetry={onRetry}
    >
      {(childrenByParent.get(stage.id) || []).length > 0 && (
        <ol className="ml-4 mt-3 space-y-2 border-l border-white/10 pl-3">
          {(childrenByParent.get(stage.id) || []).map(renderStage)}
        </ol>
      )}
    </StageItem>
  )

  return (
    <section aria-labelledby="pipeline-timeline-heading" className="glass rounded-2xl p-5">
      <h2 id="pipeline-timeline-heading">Pipeline timeline</h2>
      {sorted.length ? <ol className="mt-4 space-y-3">{sorted.filter((stage) => stage.parent_stage_id == null).map(renderStage)}</ol> : <p>No stages have been persisted.</p>}
    </section>
  )
}
