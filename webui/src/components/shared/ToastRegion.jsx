export default function ToastRegion({ toasts = [], onDismiss }) {
  const normal = toasts.filter((toast) => toast.type !== 'error')
  const errors = toasts.filter((toast) => toast.type === 'error')

  const toast = (item) => (
    <div key={item.id} className="flex items-start gap-3 rounded-lg border border-white/10 bg-[#151515] p-3 text-sm text-white shadow-lg">
      <p className="flex-1">{item.message}</p>
      <button
        type="button"
        aria-label={`Dismiss: ${item.message}`}
        onClick={() => onDismiss?.(item.id)}
        className="rounded px-2 py-1 text-gray-400 hover:bg-white/10 hover:text-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2"
      >
        Dismiss
      </button>
    </div>
  )

  return (
    <section aria-label="Notifications" className="fixed right-4 top-4 z-50 w-full max-w-sm space-y-2">
      <div role="status" aria-live="polite" aria-atomic="false" className="space-y-2">
        {normal.map(toast)}
      </div>
      {errors.map((item) => (
        <div role="alert" aria-live="assertive" key={item.id}>{toast(item)}</div>
      ))}
    </section>
  )
}
