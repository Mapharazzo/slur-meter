const CATEGORY_LABELS = {
  api_opensubtitles: 'OpenSubtitles API',
  api_tmdb: 'TMDB API',
  api_omdb: 'OMDb API',
  tts_edge: 'Edge TTS',
  tts_elevenlabs: 'ElevenLabs TTS',
  music_lyria: 'Lyria Music',
}

export default function CostBreakdown({ costs = [] }) {
  const rows = Object.values(costs.reduce((result, cost) => {
    const key = `${cost.category}:${cost.provider}`
    if (!result[key]) {
      result[key] = {
        key,
        category: cost.category,
        provider: cost.provider,
        total: 0,
        units: 0,
        count: 0,
      }
    }
    result[key].total += Number(cost.amount_usd ?? 0)
    result[key].units += Number(cost.units ?? 0)
    result[key].count += 1
    return result
  }, {}))
  const total = rows.reduce((sum, row) => sum + row.total, 0)

  return (
    <section className="glass rounded-xl p-5" aria-labelledby="cost-breakdown-heading">
      <h2 id="cost-breakdown-heading">Cost breakdown</h2>
      <p>${total.toFixed(4)} total</p>
      {rows.length ? (
        <div className="overflow-x-auto">
          <table>
            <caption>Job cost breakdown</caption>
            <thead><tr><th scope="col">Category</th><th scope="col">Provider</th><th scope="col">Calls</th><th scope="col">Units</th><th scope="col">Spend</th></tr></thead>
            <tbody>{rows.map((row) => (
              <tr key={row.key}>
                <th scope="row">{CATEGORY_LABELS[row.category] || row.category}</th>
                <td>{row.provider}</td>
                <td>{row.count}</td>
                <td>{row.units.toLocaleString()}</td>
                <td>${row.total.toFixed(4)}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      ) : <p>No job costs have been persisted.</p>}
    </section>
  )
}
