import { useState } from 'react'

import StatusBadge from '../shared/StatusBadge'
import RuntimeStrip, { displayState, words } from '../shared/RuntimeStrip'
import StageAttemptList from './StageAttemptList'

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
    return stage.state === 'running' ? <p role="status" className="hint">Progress is indeterminate.</p> : null
  }
  return (
    <div className="stage-progress">
      <progress
        aria-label={`${words(stage.name)} progress`}
        value={numerator}
        max={denominator}
        aria-valuemin="0"
        aria-valuenow={numerator}
        aria-valuemax={denominator}
      />
      <p className="micro data">{numerator} of {denominator}{unit ? ` ${unit}` : ''}</p>
    </div>
  )
}

const UNSAFE_MANIFEST_KEY = /path|secret|token|header|body/i

function StageItem({ stage, attempts, children, canRetry, busy, onRetry, expanded, onToggle }) {
  const headingId = `stage-${stage.id}`
  const manifest = Object.entries(stage.output_manifest || {}).filter(([key]) => !UNSAFE_MANIFEST_KEY.test(key))

  return (
    <li data-parent-stage={stage.parent_stage_id ?? undefined} className="stage-item">
      <div className="stage-item__head">
        <h3 id={headingId}>{words(stage.name)}</h3>
        <StatusBadge status={displayState(stage)} />
        <button
          type="button"
          className="button button--quiet button--icon button--sm"
          aria-label={`${expanded ? 'Collapse' : 'Expand'} ${words(stage.name)}`}
          aria-expanded={expanded}
          aria-controls={`${headingId}-panel`}
          onClick={onToggle}
        >
          <span aria-hidden="true">{expanded ? '−' : '+'}</span>
        </button>
      </div>
      {children}
      {expanded && (
        <div id={`${headingId}-panel`} className="stage-item__body">
          <StageProgress stage={stage} />
          <dl className="stage-facts">
            <div><dt>Retry cycle</dt><dd>Cycle {stage.retry_cycle}</dd></div>
            <div><dt>Automatic limit</dt><dd>{stage.max_auto_attempts}</dd></div>
            <div><dt>Started</dt><dd>{stage.started_at ? <time dateTime={stage.started_at}>{stage.started_at}</time> : 'Not started'}</dd></div>
            <div>
              <dt>Finished / duration</dt>
              <dd>
                {stage.finished_at ? <time dateTime={stage.finished_at}>{stage.finished_at}</time> : 'In progress'}
                {duration(stage.started_at, stage.finished_at) && <> · {duration(stage.started_at, stage.finished_at)}</>}
              </dd>
            </div>
          </dl>
          {stage.warnings?.length > 0 && (
            <section aria-label={`${words(stage.name)} warnings`} className="notice notice--warning">
              <h4>Warnings</h4>
              <ul>{stage.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
            </section>
          )}
          {stage.safe_error?.message && <p role="alert" className="inline-error">{stage.safe_error.message}</p>}
          {manifest.length > 0 && (
            <dl className="stage-facts">
              <div>
                <dt>Manifest output summary</dt>
                {manifest.map(([key, value]) => <dd key={key}>{words(key)}: {String(value)}</dd>)}
              </div>
            </dl>
          )}
          {stage.next_action && <p className="hint"><strong>Next action:</strong> {stage.next_action}</p>}
          <StageAttemptList attempts={attempts} />
          {canRetry && (
            <button type="button" className="button button--primary" disabled={busy} onClick={() => onRetry(stage)}>
              {busy ? `Retrying ${words(stage.name)}…` : `Retry ${words(stage.name)}`}
            </button>
          )}
        </div>
      )}
      {canRetry && !expanded && (
        <div className="stage-item__body">
          <button type="button" className="button button--sm" disabled={busy} onClick={() => onRetry(stage)}>
            {busy ? `Retrying ${words(stage.name)}…` : `Retry ${words(stage.name)}`}
          </button>
        </div>
      )}
    </li>
  )
}

export default function StageTimeline({ stages = [], attempts = [], availableActions = [], pendingAction, onRetry, embedded = false }) {
  const [expandedIds, setExpandedIds] = useState(() => new Set())

  const toggle = (id) => setExpandedIds((current) => {
    const next = new Set(current)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  })

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
      expanded={expandedIds.has(stage.id)}
      onToggle={() => toggle(stage.id)}
    >
      {(childrenByParent.get(stage.id) || []).length > 0 && (
        <ol className="stage-list stage-item__children">
          {(childrenByParent.get(stage.id) || []).map(renderStage)}
        </ol>
      )}
    </StageItem>
  )

  const Wrapper = embedded ? 'div' : 'section'
  const wrapperProps = embedded
    ? { className: 'stage-section' }
    : { 'aria-labelledby': 'pipeline-timeline-heading', className: 'panel panel__body' }

  return (
    <Wrapper {...wrapperProps}>
      {!embedded && <h2 id="pipeline-timeline-heading">Pipeline timeline</h2>}
      {sorted.length ? (
        <>
          {/* Clicking a segment opens that stage, so a long run does not have to
              be opened one accordion at a time to find where it stalled. */}
          <RuntimeStrip stages={sorted} onSelect={(stage) => toggle(stage.id)} />
          <ol className="stage-list">{sorted.filter((stage) => stage.parent_stage_id == null).map(renderStage)}</ol>
        </>
      ) : <p className="hint">No stages have been persisted.</p>}
    </Wrapper>
  )
}
