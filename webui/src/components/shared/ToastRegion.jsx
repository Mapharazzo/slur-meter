export default function ToastRegion({ toasts = [], onDismiss }) {
  const normal = toasts.filter((toast) => toast.type !== 'error')
  const errors = toasts.filter((toast) => toast.type === 'error')

  const toast = (item) => (
    <div key={item.id} className={`toast toast--${item.type}`}>
      <p className="flex-1">{item.message}</p>
      <button
        type="button"
        aria-label={`Dismiss: ${item.message}`}
        onClick={() => onDismiss?.(item.id)}
        className="button button--quiet button--sm"
      >
        Dismiss
      </button>
    </div>
  )

  return (
    <section aria-label="Notifications" className="toast-region">
      <div role="status" aria-live="polite" aria-atomic="false" className="grid gap-2">
        {normal.map(toast)}
      </div>
      {errors.map((item) => (
        <div role="alert" aria-live="assertive" key={item.id}>{toast(item)}</div>
      ))}
    </section>
  )
}
