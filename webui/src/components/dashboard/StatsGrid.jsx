import StatCard from '../shared/StatCard'

export default function StatsGrid({ jobs }) {
  const total = jobs.length
  const running = jobs.filter(j =>
    ['queued', 'fetching', 'analysing', 'rendering', 'encoding'].includes(j.status)
  ).length
  const done = jobs.filter(j => j.status === 'done').length
  const failed = jobs.filter(j => j.status === 'failed').length

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
      <StatCard label="Total Jobs" value={total} />
      <StatCard label="Running" value={running} color="text-blue-400" />
      <StatCard label="Completed" value={done} color="text-green-400" />
      <StatCard label="Failed" value={failed} color="text-red-400" />
    </div>
  )
}
