import StatusBadge from '../shared/StatusBadge'

const STEP_ORDER = ['fetch', 'analyse', 'graph', 'composite', 'audio', 'encode']
const STEP_LABELS = {
  fetch: 'Fetch Subtitles',
  analyse: 'Analyse Profanity',
  graph: 'Generate Graph',
  composite: 'Composite Video',
  audio: 'Audio Pipeline',
  encode: 'Encode MP4',
}

export default function PipelineSteps({ steps = [] }) {
  const stepMap = Object.fromEntries(steps.map(s => [s.step_name, s]))

  return (
    <div className="glass rounded-xl p-5 space-y-3">
      <h3 className="text-sm font-bold text-gray-400 uppercase tracking-wider">Pipeline Steps</h3>
      <div className="space-y-2">
        {STEP_ORDER.map(name => {
          const step = stepMap[name]
          const label = STEP_LABELS[name] || name
          const duration = step?.duration_ms ? `${(step.duration_ms / 1000).toFixed(1)}s` : null

          return (
            <div key={name} className="flex items-center gap-3 py-2 px-3 rounded-lg bg-white/[0.02]">
              <StatusBadge status={step?.status || 'pending'} />
              <span className="flex-1 text-sm">{label}</span>
              {duration && (
                <span className="text-xs text-gray-600 font-mono">{duration}</span>
              )}
              {step?.message && (
                <span className="text-xs text-gray-500 max-w-[200px] truncate">{step.message}</span>
              )}
            </div>
          )
        })}
      </div>

      {/* Warnings */}
      {steps.some(s => s.warnings?.length) && (
        <div className="mt-3 space-y-1">
          <h4 className="text-xs font-bold text-yellow-500">Warnings</h4>
          {steps.filter(s => s.warnings?.length).flatMap(s =>
            s.warnings.map((w, i) => (
              <div key={`${s.step_name}-${i}`} className="text-xs text-yellow-400/70 pl-3">
                {s.step_name}: {w}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  )
}
