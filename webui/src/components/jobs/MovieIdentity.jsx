import { useEffect, useRef, useState } from 'react'

import { api } from '../../api'
import { useApp } from '../../context/AppContext'

// A title query is a guess until the provider resolves it. Showing the matched
// poster and IMDb id as soon as the metadata stage lands lets an operator catch
// a wrong film here, rather than after a full analyse-and-render.
export default function MovieIdentity({
  jobId,
  stages = [],
  label,
  sourceImdbId,
  canCancel = false,
  pendingAction = null,
  onCancel,
  client = api,
}) {
  const { operatorToken } = useApp()
  const [posterUrl, setPosterUrl] = useState('')
  const objectUrl = useRef('')

  const stage = stages.find((item) => item.name === 'metadata')
  const details = stage?.output_manifest?.details || {}
  const resolved = stage?.state === 'completed' && Boolean(stage?.output_manifest)
  const posterAvailable = resolved && details.poster_file === 'poster.jpg'
  const title = details.resolved_title || null
  const year = details.resolved_year || null
  const imdbId = details.resolved_imdb_id || sourceImdbId || null

  useEffect(() => {
    if (!posterAvailable || typeof client.fetchPoster !== 'function') return undefined
    let active = true
    const controller = new AbortController()
    client
      .fetchPoster(jobId, { token: operatorToken, signal: controller.signal })
      .then((blob) => {
        if (!active) return
        const url = URL.createObjectURL(blob)
        objectUrl.current = url
        setPosterUrl(url)
      })
      .catch(() => {})
    return () => {
      active = false
      controller.abort()
      if (objectUrl.current) URL.revokeObjectURL(objectUrl.current)
      objectUrl.current = ''
      setPosterUrl('')
    }
  }, [client, jobId, operatorToken, posterAvailable])

  // Nothing useful to confirm yet on a job that already carried a canonical id.
  if (!resolved && sourceImdbId) return null

  return (
    <section className="panel identity" aria-labelledby="identity-heading">
      <div className="identity__poster">
        {posterUrl
          ? <img src={posterUrl} alt={`Poster for ${title || label}`} />
          : <span aria-hidden="true" className="identity__poster-empty">{resolved ? '🎞' : '…'}</span>}
      </div>
      <div className="identity__body">
        <p className="eyebrow">{resolved ? 'Resolved match' : 'Resolving match'}</p>
        <h2 id="identity-heading">
          {title || label}
          {year && <span className="identity__year"> ({year})</span>}
        </h2>
        {resolved ? (
          <>
            <p className="hint">
              Searched for <strong>{label}</strong>
              {imdbId && <> · matched <span className="data">{imdbId}</span></>}
            </p>
            {imdbId && (
              <p className="hint">
                <a
                  href={`https://www.imdb.com/title/${encodeURIComponent(imdbId)}/`}
                  target="_blank"
                  rel="noreferrer noopener"
                >
                  Open {imdbId} on IMDb
                </a>
              </p>
            )}
            {canCancel && (
              <div className="notice__actions">
                <button
                  type="button"
                  className="button button--danger button--sm"
                  disabled={Boolean(pendingAction)}
                  onClick={() => onCancel?.('cancel')}
                >
                  Wrong film — cancel run
                </button>
              </div>
            )}
          </>
        ) : (
          <p className="hint" role="status">
            Matching <strong>{label}</strong> against the metadata provider. The poster appears here once
            the metadata stage completes.
          </p>
        )}
      </div>
    </section>
  )
}
