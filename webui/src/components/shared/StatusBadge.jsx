const STATUS_STYLES = {
  queued:    'bg-gray-500/20 text-gray-400',
  fetching:  'bg-yellow-500/20 text-yellow-400',
  analysing: 'bg-blue-500/20 text-blue-400',
  rendering: 'bg-purple-500/20 text-purple-400',
  encoding:  'bg-indigo-500/20 text-indigo-400',
  done:      'bg-green-500/20 text-green-400',
  failed:    'bg-red-500/20 text-red-400',
  running:   'bg-blue-500/20 text-blue-400',
  pending:   'bg-gray-500/20 text-gray-500',
}

export default function StatusBadge({ status }) {
  const style = STATUS_STYLES[status] || STATUS_STYLES.pending
  return (
    <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${style}`}>
      {status}
    </span>
  )
}
