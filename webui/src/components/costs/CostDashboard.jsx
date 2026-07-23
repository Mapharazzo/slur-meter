import { useCallback, useState } from 'react'

import { api } from '../../api'
import { useApp } from '../../context/AppContext'
import { usePollingResource } from '../../hooks/usePollingResource'
import ResourceState from '../shared/ResourceState'

const GROUP_OPTIONS = [
  { key: 'category', label: 'By category' },
  { key: 'day', label: 'By day' },
  { key: 'month', label: 'By month' },
]

const CATEGORY_LABELS = {
  api_opensubtitles: 'OpenSubtitles',
  api_tmdb: 'TMDB',
  api_omdb: 'OMDb',
  tts_edge: 'Edge TTS',
  tts_elevenlabs: 'ElevenLabs',
  music_lyria: 'Lyria Music',
}

export default function CostDashboard({ client = api, pollingOptions = {} }) {
  const { operatorToken } = useApp()
  const [groupBy, setGroupBy] = useState('category')
  const load = useCallback(
    (signal) => client.getAggregateCosts(
      { group_by: groupBy },
      { token: operatorToken, signal },
    ),
    [client, groupBy, operatorToken],
  )
  const resource = usePollingResource(load, {
    intervalMs: 30_000,
    staleAfterMs: 90_000,
    dependencies: [operatorToken, groupBy],
    ...pollingOptions,
  })

  const renderTable = (data) => {
    const grandTotal = data.reduce((sum, row) => sum + Number(row.total_usd ?? 0), 0)
    const totalUnits = data.reduce((sum, row) => sum + Number(row.total_units ?? 0), 0)
    const calls = data.reduce((sum, row) => sum + Number(row.count ?? 0), 0)
    return (
      <>
        <div className="grid grid-cols-3 gap-4">
          <p><strong>${grandTotal.toFixed(4)}</strong><br />Total spend</p>
          <p><strong>{totalUnits.toLocaleString()}</strong><br />Total units</p>
          <p><strong>{calls.toLocaleString()}</strong><br />Calls</p>
        </div>
        <div className="glass overflow-x-auto rounded-xl">
          <table className="w-full">
            <caption>Aggregate operational costs</caption>
            <thead><tr>
              <th scope="col">{groupBy === 'category' ? 'Category' : 'Period'}</th>
              <th scope="col">Provider</th>
              <th scope="col">Units</th>
              <th scope="col">Calls</th>
              <th scope="col">Spend</th>
            </tr></thead>
            <tbody>{data.map((row) => (
              <tr key={`${row.period}:${row.category}:${row.provider}`}>
                <th scope="row">{groupBy === 'category' ? (CATEGORY_LABELS[row.category] || row.category) : row.period}</th>
                <td>{row.provider}</td>
                <td>{Number(row.total_units ?? 0).toLocaleString()}</td>
                <td>{Number(row.count ?? 0).toLocaleString()}</td>
                <td>${Number(row.total_usd ?? 0).toFixed(4)}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      </>
    )
  }

  return (
    <section className="max-w-4xl mx-auto space-y-6" aria-labelledby="cost-dashboard-heading">
      <header>
        <h1 id="cost-dashboard-heading">Cost dashboard</h1>
        <div role="group" aria-label="Group costs">
          {GROUP_OPTIONS.map((option) => (
            <button
              type="button"
              key={option.key}
              aria-pressed={groupBy === option.key}
              onClick={() => setGroupBy(option.key)}
            >
              {option.label}
            </button>
          ))}
        </div>
      </header>
      <ResourceState
        resource={resource}
        loadingMessage="Loading cost aggregates…"
        emptyMessage="No cost data has been persisted."
        isEmpty={(data) => !data?.length}
      >
        {renderTable}
      </ResourceState>
    </section>
  )
}
