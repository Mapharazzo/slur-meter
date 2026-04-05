export default function RevenueDashboard() {
  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-bold">Revenue & Views</h2>
        <span className="bg-yellow-500/20 text-yellow-400 text-xs font-medium px-2.5 py-1 rounded-full">
          Coming Soon
        </span>
      </div>

      {/* Placeholder stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {[
          { label: 'Total Views', value: '—', sub: 'all platforms' },
          { label: 'Revenue', value: '—', sub: 'all time' },
          { label: 'YouTube Shorts', value: '—', sub: 'videos uploaded' },
          { label: 'TikTok', value: '—', sub: 'videos uploaded' },
        ].map(card => (
          <div key={card.label} className="glass rounded-xl p-4 text-center opacity-50">
            <div className="text-2xl font-bold text-gray-400">{card.value}</div>
            <div className="text-xs text-gray-600 mt-1">{card.label}</div>
            <div className="text-xs text-gray-700">{card.sub}</div>
          </div>
        ))}
      </div>

      {/* Placeholder chart area */}
      <div className="glass rounded-xl p-8 text-center space-y-3 border border-dashed border-white/10">
        <div className="text-3xl opacity-30">📊</div>
        <p className="text-gray-600 text-sm">
          Revenue and views data will appear here once YouTube and TikTok publishing is connected.
        </p>
        <p className="text-gray-700 text-xs">
          The database schema is ready — wire up the platform APIs to start tracking.
        </p>
      </div>

      {/* Platforms */}
      <div className="grid grid-cols-2 gap-4">
        {[
          { name: 'YouTube Shorts', status: 'Not connected', icon: '▶️' },
          { name: 'TikTok', status: 'Not connected', icon: '🎵' },
        ].map(platform => (
          <div key={platform.name} className="glass rounded-xl p-4 flex items-center gap-3 opacity-60">
            <span className="text-2xl">{platform.icon}</span>
            <div>
              <div className="font-medium text-sm">{platform.name}</div>
              <div className="text-xs text-gray-600">{platform.status}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
