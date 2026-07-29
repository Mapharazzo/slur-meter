// The pipeline drawn as a timeline rather than a list. Segment width follows a
// stage's real elapsed duration, so a slow stage looks slow and an operator can
// read where a run spent its time without expanding anything.

// Mirrors GENERATION_STAGES in api/pipeline.py. Used only to sketch a run's
// shape in the queue, where the list endpoint returns a current stage but no
// stage records; the run workspace always draws from persisted stages.
export const GENERATION_STAGES = [
  'input_resolution',
  'subtitle_discovery',
  'metadata',
  'subtitle_selection',
  'analysis',
  'graph',
  'composite',
  'audio',
  'encode',
]

// Anything shorter than this reads as a sliver, which keeps sub-second stages
// visible without letting them distort the proportions of a long run.
const MIN_WEIGHT_SECONDS = 3

export function words(value) {
  return String(value || '').replaceAll('.', ' ').replaceAll('_', ' ')
}

export function elapsedSeconds(startedAt, finishedAt) {
  if (!startedAt) return 0
  const start = Date.parse(startedAt)
  if (Number.isNaN(start)) return 0
  const end = finishedAt ? Date.parse(finishedAt) : Date.now()
  if (Number.isNaN(end)) return 0
  return Math.max(0, (end - start) / 1000)
}

// Compact clock reading for the strip: 45s, 2m 05s, 1h 12m.
export function clock(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return '—'
  const total = Math.round(seconds)
  if (total < 60) return `${total}s`
  const minutes = Math.floor(total / 60)
  if (minutes < 60) return `${minutes}m ${String(total % 60).padStart(2, '0')}s`
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`
}

// A composite child stays `running` at full progress until its parent's artifact
// is durably promoted, so show a fully-rendered child as completed instead of a
// stale spinner.
export function displayState(stage) {
  const { numerator, denominator } = stage.progress || {}
  const complete = denominator > 0 && numerator != null && numerator >= denominator
  if (stage.parent_stage_id != null && stage.state === 'running' && complete) return 'completed'
  return stage.state
}

/**
 * Queue variant: a thin, decorative sketch of how far a run has travelled.
 * The row already states the run's state and stage in text, so this carries no
 * accessible name of its own.
 */
export function RunTrack({ state, currentStage }) {
  const position = GENERATION_STAGES.indexOf(String(currentStage || ''))
  const finished = state === 'completed'

  return (
    <span className="track track--thin" aria-hidden="true">
      {GENERATION_STAGES.map((name, index) => {
        let segmentState = 'pending'
        if (finished || (position >= 0 && index < position)) segmentState = 'completed'
        else if (position >= 0 && index === position) segmentState = state
        return <span key={name} className="track__seg" data-state={segmentState} style={{ flex: 1 }} />
      })}
    </span>
  )
}

/**
 * Workspace variant: proportional, selectable segments built from persisted
 * stages. Selecting a segment scrolls the matching stage into view instead of
 * making the operator hunt through a stack of accordions.
 */
export default function RuntimeStrip({ stages = [], selectedId = null, onSelect }) {
  const top = [...stages]
    .filter((stage) => stage.parent_stage_id == null)
    .sort((left, right) => left.ordinal - right.ordinal || left.id - right.id)

  if (!top.length) return null

  const measured = top.map((stage) => {
    const seconds = elapsedSeconds(stage.started_at, stage.finished_at)
    return { stage, seconds, weight: Math.max(seconds, MIN_WEIGHT_SECONDS) }
  })
  const totalSeconds = measured.reduce((sum, item) => sum + item.seconds, 0)
  const slowest = measured.reduce(
    (worst, item) => (item.seconds > worst.seconds ? item : worst),
    measured[0],
  )

  return (
    <div className="runtime-strip">
      <div className="track track--tall" role="group" aria-label="Pipeline timeline by stage duration">
        {measured.map(({ stage, seconds, weight }) => {
          const state = displayState(stage)
          const label = `${words(stage.name)} · ${state.replaceAll('_', ' ')} · ${clock(seconds)}`
          return (
            <button
              key={stage.id}
              type="button"
              className="track__seg"
              data-state={state}
              style={{ flex: weight }}
              title={label}
              aria-label={label}
              aria-pressed={selectedId === stage.id}
              onClick={() => onSelect?.(stage)}
            />
          )
        })}
      </div>
      <p className="runtime-strip__readout">
        <span>{clock(totalSeconds)} total</span>
        {slowest.seconds > 0 && (
          <span>
            slowest <b>{words(slowest.stage.name)}</b> {clock(slowest.seconds)}
          </span>
        )}
        <span>{top.length} stages</span>
      </p>
    </div>
  )
}
