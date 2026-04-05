const CATEGORY_LABELS = {
  api_opensubtitles: 'OpenSubtitles API',
  api_tmdb: 'TMDB API',
  api_omdb: 'OMDb API',
  tts_edge: 'Edge TTS',
  tts_elevenlabs: 'ElevenLabs TTS',
  music_lyria: 'Lyria Music',
}

export default function CostBreakdown({ costs = [] }) {
  const total = costs.reduce((sum, c) => sum + (c.amount_usd || 0), 0)

  const byCategory = costs.reduce((acc, c) => {
    const key = c.category
    if (!acc[key]) acc[key] = { total: 0, units: 0, provider: c.provider, count: 0 }
    acc[key].total += c.amount_usd || 0
    acc[key].units += c.units || 0
    acc[key].count += 1
    return acc
  }, {})

  return (
    <div className="glass rounded-xl p-5 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-bold text-gray-400 uppercase tracking-wider">Cost Breakdown</h3>
        <span className="text-xs font-mono text-green-400">
          ${total.toFixed(4)} total
        </span>
      </div>

      <div className="space-y-2">
        {Object.entries(byCategory).map(([cat, data]) => (
          <div key={cat} className="flex items-center gap-3 py-1.5 px-3 rounded-lg bg-white/[0.02]">
            <div className="flex-1">
              <div className="text-sm">{CATEGORY_LABELS[cat] || cat}</div>
              <div className="text-xs text-gray-600">{data.count} call{data.count !== 1 ? 's' : ''} · {data.units.toLocaleString()} units</div>
            </div>
            <span className="text-xs font-mono text-gray-400">
              ${data.total.toFixed(4)}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
