import { useState, useEffect } from 'react'
import StatusBadge from '../shared/StatusBadge'
import { api } from '../../api'

const STEP_ORDER = ['fetch', 'analyse', 'graph', 'composite', 'audio', 'encode']
const STEP_LABELS = {
  fetch: 'Fetch Subtitles',
  analyse: 'Analyse Profanity',
  graph: 'Generate Graph',
  composite: 'Composite Video',
  audio: 'Audio Pipeline',
  encode: 'Encode MP4',
}

export default function PipelineSteps({ steps = [], imdbId, isActive }) {
  const stepMap = Object.fromEntries(steps.map(s => [s.step_name, s]))
  const [timestamp, setTimestamp] = useState(Date.now())

  useEffect(() => {
    if (!isActive) return
    const id = setInterval(() => setTimestamp(Date.now()), 1500)
    return () => clearInterval(id)
  }, [isActive])

  return (
    <div className="glass rounded-xl p-5 space-y-3">
      <h3 className="text-sm font-bold text-gray-400 uppercase tracking-wider">Pipeline Steps</h3>
      <div className="space-y-2">
        {STEP_ORDER.map(name => {
          const step = stepMap[name]
          const label = STEP_LABELS[name] || name
          const duration = step?.duration_ms ? `${(step.duration_ms / 1000).toFixed(1)}s` : null

          let cleanMessage = step?.message || ''
          let progressPercent = null
          
          if (cleanMessage) {
            const progMatch = cleanMessage.match(/\s*\[(\d+)\/(\d+)\]$/)
            if (progMatch) {
              cleanMessage = cleanMessage.replace(progMatch[0], '')
              const curr = parseInt(progMatch[1], 10)
              const tot = parseInt(progMatch[2], 10)
              if (tot > 0) progressPercent = (curr / tot) * 100
            }
          }

          return (
            <div 
              key={name} 
              className={`flex items-start gap-3 py-2.5 px-3 rounded-lg transition-colors ${
                step?.status === 'running' ? 'bg-white/10 ring-1 ring-white/20' : 'bg-white/[0.02]'
              }`}
            >
              <div className="pt-0.5">
                <StatusBadge status={step?.status || 'pending'} />
              </div>
              
              <div className="flex-1 flex flex-col min-w-0">
                <div className="flex justify-between items-center">
                  <span className="text-sm truncate">{label}</span>
                  {duration && (
                    <span className="text-xs text-gray-600 font-mono pl-2 shrink-0">{duration}</span>
                  )}
                </div>
                {cleanMessage && (
                  <span className="text-xs text-gray-400 truncate mt-0.5 max-w-[250px]">{cleanMessage}</span>
                )}
                {progressPercent !== null && (
                  <div className="w-full bg-black/40 h-1.5 rounded-full mt-1.5 overflow-hidden">
                    <div 
                      className="h-full bg-gradient-to-r from-teal-400 to-emerald-500 transition-all duration-300"
                      style={{ width: `${progressPercent}%` }}
                    />
                  </div>
                )}
              </div>

              {(name === 'graph' || name === 'composite') && step?.status === 'running' && (
                <div className="w-10 h-16 ml-3 bg-black/50 rounded overflow-hidden shrink-0 border border-white/10">
                  <img 
                    src={`${api.previewUrl(imdbId)}?t=${timestamp}`} 
                    className="w-full h-full object-cover transition-opacity duration-300 opacity-0"
                    onError={(e) => { e.target.style.opacity = '0' }}
                    onLoad={(e) => { e.target.style.opacity = '1' }}
                    alt="preview"
                  />
                </div>
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
