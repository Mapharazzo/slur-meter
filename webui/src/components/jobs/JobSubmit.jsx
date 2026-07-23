import { useId, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api, createIdempotencyKey } from '../../api'
import { useApp } from '../../context/AppContext'

const IMDB_ID = /^tt\d{7,10}$/

export default function JobSubmit({ client = api }) {
  const [mode, setMode] = useState('query')
  const [value, setValue] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const inFlight = useRef(false)
  const errorId = useId()
  const navigate = useNavigate()
  const { operatorToken, addToast } = useApp()

  const changeMode = (nextMode) => {
    setMode(nextMode)
    setValue('')
    setError('')
  }

  const handleSubmit = async (event) => {
    event.preventDefault()
    if (inFlight.current) return
    const normalized = value.trim()
    if (!normalized) {
      setError('Enter one movie title or canonical IMDb ID.')
      return
    }
    if (mode === 'imdb' && !IMDB_ID.test(normalized)) {
      setError('Use a canonical IMDb ID such as tt0110912.')
      return
    }

    inFlight.current = true
    setSubmitting(true)
    setError('')
    const idempotencyKey = createIdempotencyKey()
    try {
      const body = mode === 'imdb' ? { imdb_id: normalized } : { query: normalized }
      const created = await client.submitJob(body, { token: operatorToken, idempotencyKey })
      setValue('')
      addToast({ type: 'success', message: `Run created for ${created.label || 'movie'}.` })
      navigate(`/jobs/${encodeURIComponent(created.id)}`)
    } catch (cause) {
      setError(cause?.message || 'The run could not be created.')
    } finally {
      inFlight.current = false
      setSubmitting(false)
    }
  }

  const inputLabel = mode === 'imdb' ? 'Canonical IMDb ID' : 'Movie title or query'

  return (
    <section className="create-panel" aria-labelledby="create-run-heading">
      <div>
        <p className="eyebrow">New operation</p>
        <h2 id="create-run-heading">Create a run</h2>
        <p>Resolve exactly one movie query or canonical IMDb identity.</p>
      </div>
      <form onSubmit={handleSubmit} noValidate>
        <fieldset className="mode-switch">
          <legend>Input mode</legend>
          <label>
            <input type="radio" name="input-mode" value="query" checked={mode === 'query'} onChange={() => changeMode('query')} />
            Movie title or query
          </label>
          <label>
            <input type="radio" name="input-mode" value="imdb" checked={mode === 'imdb'} onChange={() => changeMode('imdb')} />
            Canonical IMDb ID
          </label>
        </fieldset>
        <div className="create-panel__controls">
          <label className="field-label">
            <span>{inputLabel}</span>
            <input
              type="text"
              value={value}
              onChange={(event) => { setValue(event.target.value); setError('') }}
              aria-describedby={error ? errorId : undefined}
              aria-invalid={Boolean(error)}
              autoComplete="off"
              placeholder={mode === 'imdb' ? 'tt0110912' : 'Pulp Fiction'}
            />
          </label>
          <button type="submit" className="button button--primary" disabled={submitting}>
            {submitting ? 'Creating run…' : 'Create run'}
          </button>
        </div>
        {error && <p id={errorId} role="alert" className="inline-error">{error}</p>}
      </form>
    </section>
  )
}
