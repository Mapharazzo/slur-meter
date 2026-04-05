import { useState, useEffect } from 'react'
import { api } from '../../api'

const GROUP_OPTIONS = [
  { key: 'category', label: 'By Category' },
  { key: 'day', label: 'By Day' },
  { key: 'month', label: 'By Month' },
]

const CATEGORY_LABELS = {
  api_opensubtitles: 'OpenSubtitles',
  api_tmdb: 'TMDB',
  api_omdb: 'OMDb',
  tts_edge: 'Edge TTS',
  tts_elevenlabs: 'ElevenLabs',
  music_lyria: 'Lyria Music',
}

export default function CostDashboard() {
  const [data, setData] = useState([])
  const [groupBy, setGroupBy] = useState('category')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    api.getAggregateCosts({ group_by: groupBy })
      .then(setData)
      .catch(() => setData([]))
      .finally(() => setLoading(false))
  }, [groupBy])

  const grandTotal = data.reduce((sum, r) => sum + (r.total_usd || 0), 0)
  const totalUnits = data.reduce((sum, r) => sum + (r.total_units || 0), 0)

  // Aggregate to unique periods/categories for display
  const rows = data.reduce((acc, r) => {
    const key = r.period || r.category
    if (!acc[key]) acc[key] = { label: key, total_usd: 0, total_units: 0, count: 0 }
    acc[key].total_usd += r.total_usd || 0
    acc[key].total_units += r.total_units || 0
    acc[key].count += r.count || 0
    return acc
  }, {})

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-bold">Cost Dashboard</h2>
        <div className="flex gap-2">
          {GROUP_OPTIONS.map(opt => (
            <button
              key={opt.key}
              onClick={() => setGroupBy(opt.key)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-all ${
                groupBy === opt.key ? 'bg-white/10 text-white' : 'text-gray-500 hover:text-white'
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-3 gap-4">
        <div className="glass rounded-xl p-4 text-center">
          <div className="text-2xl font-bold text-green-400">${grandTotal.toFixed(4)}</div>
          <div className="text-xs text-gray-500 mt-1">Total Spend</div>
        </div>
        <div className="glass rounded-xl p-4 text-center">
          <div className="text-2xl font-bold text-white">{totalUnits.toLocaleString()}</div>
          <div className="text-xs text-gray-500 mt-1">Total Units</div>
        </div>
        <div className="glass rounded-xl p-4 text-center">
          <div className="text-2xl font-bold text-white">{Object.keys(rows).length}</div>
          <div className="text-xs text-gray-500 mt-1">{groupBy === 'category' ? 'Categories' : 'Periods'}</div>
        </div>
      </div>

      {/* Table */}
      <div className="glass rounded-xl overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-white/5">
              <th className="text-left p-4 text-gray-500 font-medium text-xs uppercase tracking-wider">
                {groupBy === 'category' ? 'Category' : 'Period'}
              </th>
              <th className="text-right p-4 text-gray-500 font-medium text-xs uppercase tracking-wider">Units</th>
              <th className="text-right p-4 text-gray-500 font-medium text-xs uppercase tracking-wider">Calls</th>
              <th className="text-right p-4 text-gray-500 font-medium text-xs uppercase tracking-wider">Spend</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={4} className="text-center py-8 text-gray-600">Loading...</td>
              </tr>
            ) : Object.entries(rows).length === 0 ? (
              <tr>
                <td colSpan={4} className="text-center py-8 text-gray-600">No cost data yet</td>
              </tr>
            ) : (
              Object.entries(rows).map(([key, row]) => (
                <tr key={key} className="border-b border-white/[0.04] hover:bg-white/[0.02]">
                  <td className="p-4">
                    {CATEGORY_LABELS[row.label] || row.label}
                  </td>
                  <td className="p-4 text-right font-mono text-gray-400">
                    {row.total_units.toLocaleString()}
                  </td>
                  <td className="p-4 text-right font-mono text-gray-500">
                    {row.count}
                  </td>
                  <td className="p-4 text-right font-mono text-green-400">
                    ${row.total_usd.toFixed(4)}
                  </td>
                </tr>
              ))
            )}
          </tbody>
          {Object.keys(rows).length > 0 && (
            <tfoot>
              <tr className="border-t border-white/10">
                <td className="p-4 font-bold text-gray-400">Total</td>
                <td className="p-4 text-right font-mono font-bold">{totalUnits.toLocaleString()}</td>
                <td />
                <td className="p-4 text-right font-mono font-bold text-green-400">
                  ${grandTotal.toFixed(4)}
                </td>
              </tr>
            </tfoot>
          )}
        </table>
      </div>

      <p className="text-xs text-gray-700 text-center">
        Note: Free-tier APIs (TMDB, OpenSubtitles, Edge TTS) show $0.00. Costs apply if you enable ElevenLabs or Lyria.
      </p>
    </div>
  )
}
