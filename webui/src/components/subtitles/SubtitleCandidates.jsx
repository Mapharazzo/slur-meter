import { useState } from 'react'

import { api, createIdempotencyKey } from '../../api'

function display(value, suffix = '') {
  return value == null ? 'Not available' : `${value}${suffix}`
}

function seconds(value) {
  if (value == null) return 'Not available'
  const total = Math.round(value)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const rest = total % 60
  return [hours && `${hours}h`, minutes && `${minutes}m`, `${rest}s`].filter(Boolean).join(' ')
}

export default function SubtitleCandidates({
  jobId,
  token,
  candidates = [],
  availableActions = [],
  client = api,
  onRefresh = async () => {},
  actionRunner,
  pendingAction: sharedPending = null,
  embedded = false,
}) {
  const [pending, setPending] = useState(null)
  const [error, setError] = useState('')
  const [file, setFile] = useState(null)

  const mutate = async (name, call) => {
    if (sharedPending || pending) return
    if (actionRunner) {
      setError('')
      const result = await actionRunner(name, call, { announceGlobal: false })
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
      setError(failure?.message || 'The subtitle action failed.')
    } finally {
      setPending(null)
    }
  }
  const activePending = sharedPending || pending

  const upload = () => {
    if (!file || !file.name.toLowerCase().endsWith('.srt')) {
      setError('Choose an .srt file before uploading.')
      return
    }
    mutate('upload', (options) => client.uploadSubtitle(jobId, file, options))
  }

  const Wrapper = embedded ? 'div' : 'section'
  const wrapperProps = embedded
    ? { className: 'space-y-2' }
    : { 'aria-labelledby': 'subtitle-candidates-heading', className: 'glass rounded-2xl p-5' }

  return (
    <Wrapper {...wrapperProps}>
      {!embedded && <h2 id="subtitle-candidates-heading">Subtitle candidates</h2>}
      <p>Acceptance threshold: 70%. Coverage at or above 70% meets the configured duration threshold; the server remains authoritative.</p>
      {error && <p role="alert" className="inline-error">{error}</p>}
      {candidates.length ? (
        <div className="mt-4 overflow-x-auto">
          <table>
            <caption>Durable subtitle candidate comparison</caption>
            <thead><tr><th scope="col">Rank / source</th><th scope="col">Match data</th><th scope="col">Duration and coverage</th><th scope="col">Status and reasons</th><th scope="col">Selection method</th><th scope="col">Action</th></tr></thead>
            <tbody>
              {candidates.map((candidate) => {
                const canSelect = availableActions.includes(`select_subtitle:${candidate.id}`)
                const candidateLabel = candidate.provider_filename || candidate.provider_id || candidate.id
                return (
                  <tr key={candidate.id} aria-label={candidateLabel}>
                    <td>#{display(candidate.rank)}<br />{candidate.provider} · {candidate.source_type}</td>
                    <td>
                      {candidate.title || 'Unknown title'} {candidate.year || ''}<br />
                      IMDb {candidate.imdb_match == null ? 'unknown' : candidate.imdb_match ? 'match' : 'mismatch'} · {candidate.language || 'unknown language'}
                      {candidate.rank_reasons?.length > 0 && <ul>{candidate.rank_reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>}
                    </td>
                    <td>
                      Parsed duration: {seconds(candidate.parsed_duration_seconds)}<br />
                      Expected duration: {seconds(candidate.expected_runtime_seconds)}<br />
                      Coverage: <span>{display(candidate.coverage_percent, '%')}</span>
                    </td>
                    <td>
                      <strong>{candidate.status}</strong>
                      {[...(candidate.quality_reasons || []), ...(candidate.rejection_reasons || [])].length > 0 && (
                        <ul>{[...(candidate.quality_reasons || []), ...(candidate.rejection_reasons || [])].map((reason) => <li key={reason}>{reason}</li>)}</ul>
                      )}
                    </td>
                    <td>{candidate.selection_method || 'Not selected'}</td>
                    <td>{canSelect && <button type="button" className="button" disabled={Boolean(activePending)} onClick={() => mutate(`select:${candidate.id}`, (options) => client.selectSubtitle(jobId, candidate.id, options))}>Select candidate {candidate.rank ?? candidate.id}</button>}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ) : <p>No subtitle candidates have been returned.</p>}

      <div className="mt-4 flex flex-wrap items-end gap-3">
        {availableActions.includes('rediscover_subtitles') && <button type="button" className="button" disabled={Boolean(activePending)} onClick={() => mutate('rediscover', (options) => client.rediscoverSubtitles(jobId, options))}>Rediscover subtitles</button>}
        <label className="field-label"><span>Upload SRT</span><input type="file" accept=".srt,application/x-subrip" onChange={(event) => { setFile(event.target.files?.[0] || null); setError('') }} /></label>
        <button type="button" className="button" disabled={Boolean(activePending)} onClick={upload}>Upload subtitle</button>
        {availableActions.includes('resume') && <button type="button" className="button button--primary" disabled={Boolean(activePending)} onClick={() => mutate('resume', (options) => client.resumeJob(jobId, options))}>Resume run</button>}
      </div>
    </Wrapper>
  )
}
