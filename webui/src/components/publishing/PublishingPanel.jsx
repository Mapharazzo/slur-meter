import { useState } from 'react'

import { api, createIdempotencyKey } from '../../api'

const PLATFORM_ORDER = ['youtube', 'tiktok', 'instagram']

function words(value) {
  return String(value || 'unknown').replaceAll('_', ' ')
}

function attemptDuration(attempt) {
  if (!attempt.started_at || !attempt.finished_at) return null
  const seconds = Math.max(0, Math.round((Date.parse(attempt.finished_at) - Date.parse(attempt.started_at)) / 1000))
  return Number.isFinite(seconds) ? `${seconds} second${seconds === 1 ? '' : 's'}` : null
}

export default function PublishingPanel({
  jobId,
  token,
  releases = [],
  publishingAttempts = [],
  availableActions = [],
  client = api,
  onRefresh = async () => {},
  actionRunner,
  pendingAction: sharedPending = null,
}) {
  const [pending, setPending] = useState(null)
  const [error, setError] = useState('')
  const [remoteIds, setRemoteIds] = useState({})

  const mutate = async (name, call) => {
    if (sharedPending || pending) return
    if (actionRunner) {
      setError('')
      const result = await actionRunner(name, call)
      if (result?.ok === false && result.error) setError(result.error)
      return
    }
    setPending(name)
    setError('')
    const idempotencyKey = createIdempotencyKey()
    try {
      await call({ token, idempotencyKey })
      await onRefresh()
    } catch (failure) {
      setError(failure?.message || 'The publishing action failed.')
    } finally {
      setPending(null)
    }
  }
  const activePending = sharedPending || pending

  const platforms = PLATFORM_ORDER.filter((platform) => (
    releases.some((release) => release.platform === platform)
    || publishingAttempts.some((attempt) => attempt.platform === platform)
    || availableActions.some((action) => action.endsWith(`:${platform}`))
  ))

  const reconcileUploaded = (platform) => {
    const remoteId = (remoteIds[platform] || '').trim()
    if (!remoteId) {
      setError(`Enter the remote ID before confirming ${platform} as uploaded.`)
      return
    }
    if (!/^[\p{L}\p{N}._:-]+$/u.test(remoteId) || /[\u0000-\u001f\u007f]/.test(remoteId)) {
      setError('The remote ID contains unsupported characters.')
      return
    }
    mutate(`reconcile:${platform}`, (options) => client.reconcilePublish(jobId, platform, { reconciliation: 'uploaded', remote_id: remoteId }, options))
  }

  return (
    <section aria-labelledby="publishing-heading" className="glass rounded-2xl p-5">
      <h2 id="publishing-heading">Publishing</h2>
      <p>Publishing occurs only after a deliberate privileged operator action.</p>
      {error && <p role="alert" className="inline-error">{error}</p>}
      {platforms.length ? (
        <div className="mt-4 grid gap-4">
          {platforms.map((platform) => {
            const release = releases.find((item) => item.platform === platform)
            const attempts = publishingAttempts.filter((attempt) => attempt.platform === platform)
            const uploaded = release?.status === 'uploaded' && Boolean(release.remote_id)
            const ambiguous = release?.status === 'needs_attention' || (release?.status === 'uploaded' && !release.remote_id)
            const canPublish = !release && availableActions.includes(`publish:${platform}`)
            const canRetry = release?.status === 'failed' && availableActions.includes(`retry_publish:${platform}`)
            const canReconcile = release?.status === 'needs_attention' && availableActions.includes(`reconcile_publish:${platform}`)
            return (
              <section key={platform} aria-label={`${platform} publishing`} className="rounded-xl border border-white/10 p-4">
                <h3 className="capitalize">{platform}</h3>
                <p><strong>Status:</strong> {uploaded ? 'Uploaded complete' : ambiguous ? 'Outcome unknown — attention required' : words(release?.status || 'not requested')}</p>
                {release?.remote_id && <p><strong>Remote ID:</strong> {release.remote_id}</p>}
                {release?.uploaded_at && <p><strong>Uploaded:</strong> <time dateTime={release.uploaded_at}>{release.uploaded_at}</time></p>}
                {release?.safe_error?.message && <p>{release.safe_error.message}</p>}
                {ambiguous && (
                  <div role="alert" className="mt-3 rounded-lg border border-amber-400/30 p-3">
                    <p>{release.safe_error?.message || 'The uploaded status has no remote ID. The outcome is unknown and needs attention.'}</p>
                    {canReconcile && <>
                      <label className="field-label"><span>{platform} remote ID</span><input value={remoteIds[platform] || ''} onChange={(event) => { setRemoteIds((current) => ({ ...current, [platform]: event.target.value })); setError('') }} /></label>
                      <div className="mt-2 flex gap-2">
                        <button type="button" className="button" disabled={Boolean(activePending)} onClick={() => reconcileUploaded(platform)}>Confirm uploaded</button>
                        <button type="button" className="button" disabled={Boolean(activePending)} onClick={() => mutate(`reconcile:${platform}`, (options) => client.reconcilePublish(jobId, platform, { reconciliation: 'not_uploaded' }, options))}>Confirm not uploaded</button>
                      </div>
                    </>}
                  </div>
                )}
                {attempts.length > 0 && (
                  <section aria-label={`${platform} publishing attempts`} className="mt-3">
                    <h4>Publishing attempts</h4>
                    <ol>{attempts.map((attempt) => (
                      <li key={attempt.id} className="mt-2 rounded-lg bg-white/[0.03] p-3 text-sm">
                        <p>Cycle {attempt.retry_cycle} · Attempt {attempt.attempt_number} of {attempt.max_attempts} · {words(attempt.trigger)}</p>
                        <p>{words(attempt.outcome)} · {attempt.retryable ? 'Retryable' : 'Not retryable'}{attemptDuration(attempt) ? ` · ${attemptDuration(attempt)}` : ''}</p>
                        <p>
                          <time dateTime={attempt.started_at}>{attempt.started_at}</time>
                          {attempt.finished_at && <> — <time dateTime={attempt.finished_at}>{attempt.finished_at}</time></>}
                        </p>
                        {attempt.safe_error?.message && <p>{attempt.safe_error.message}</p>}
                        {attempt.remote_id && <p>Remote ID: {attempt.remote_id}</p>}
                      </li>
                    ))}</ol>
                  </section>
                )}
                <div className="mt-3">
                  {canPublish && <button type="button" className="button button--primary" disabled={Boolean(activePending)} onClick={() => mutate(`publish:${platform}`, (options) => client.publish(jobId, platform, options))}>Publish {platform}</button>}
                  {canRetry && <button type="button" className="button button--primary" disabled={Boolean(activePending)} onClick={() => mutate(`retry:${platform}`, (options) => client.retryPublish(jobId, platform, options))}>Retry {platform} publishing</button>}
                </div>
              </section>
            )
          })}
        </div>
      ) : <p>No platforms have been requested.</p>}
      {availableActions.includes('refresh_stats') && <button type="button" className="button mt-4" disabled={Boolean(activePending)} onClick={() => mutate('refresh_stats', (options) => client.refreshStats(jobId, options))}>Refresh platform stats</button>}
    </section>
  )
}
