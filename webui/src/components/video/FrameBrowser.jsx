import { useCallback, useEffect, useRef, useState } from 'react'

import { api } from '../../api'
import { useApp } from '../../context/AppContext'

export default function FrameBrowser({ jobId: jobIdProp, imdbId, segment, client = api }) {
  const jobId = jobIdProp ?? imdbId
  const { operatorToken } = useApp()
  const [info, setInfo] = useState(null)
  const [frame, setFrame] = useState(0)
  const [status, setStatus] = useState('loading')
  const [error, setError] = useState('')
  const [url, setUrl] = useState('')
  const generation = useRef(0)
  const currentUrl = useRef('')

  const revokeUrl = useCallback(() => {
    if (currentUrl.current) URL.revokeObjectURL(currentUrl.current)
    currentUrl.current = ''
    setUrl('')
  }, [])

  useEffect(() => {
    const ownGeneration = ++generation.current
    const controller = new AbortController()
    revokeUrl()
    setFrame(0)
    setInfo(null)
    setError('')
    setStatus('loading')
    client.getSegmentInfo(jobId, segment, { token: operatorToken, signal: controller.signal })
      .then((value) => {
        if (generation.current !== ownGeneration || controller.signal.aborted) return
        setInfo(value)
        setStatus('success')
      })
      .catch((failure) => {
        if (generation.current !== ownGeneration || controller.signal.aborted) return
        setError(failure?.message || 'The frame browser could not be loaded.')
        setStatus('error')
      })
    return () => {
      controller.abort()
      generation.current += 1
      revokeUrl()
    }
  }, [client, jobId, operatorToken, revokeUrl, segment])

  useEffect(() => {
    if (!info?.frame_count) return undefined
    const ownGeneration = generation.current
    const controller = new AbortController()
    revokeUrl()
    setError('')
    setStatus('loading-frame')
    client.fetchFrame(jobId, segment, frame, { token: operatorToken, signal: controller.signal })
      .then((blob) => {
        if (generation.current !== ownGeneration || controller.signal.aborted) return
        const objectUrl = URL.createObjectURL(blob)
        currentUrl.current = objectUrl
        setUrl(objectUrl)
        setStatus('success')
      })
      .catch((failure) => {
        if (generation.current !== ownGeneration || controller.signal.aborted) return
        setError(failure?.message || 'The frame could not be loaded.')
        setStatus('frame-error')
      })
    return () => controller.abort()
  }, [client, frame, info, jobId, operatorToken, revokeUrl, segment])

  if (status === 'loading') return <div role="status">Loading frame browser…</div>
  if (status === 'error') return <div role="alert">{error}</div>
  if (!info?.frame_count) return <p>No frames are available for this segment.</p>

  const maxFrame = info.frame_count - 1
  const frameLabel = `${segment.replaceAll('_', ' ')} frame ${frame + 1} of ${info.frame_count}`
  return (
    <section aria-label={`${segment.replaceAll('_', ' ')} frame browser`} className="glass rounded-xl p-4 space-y-3">
      <h3>Frame browser</h3>
      <div className="flex items-center gap-3">
        <button type="button" aria-label="Previous frame" onClick={() => setFrame((value) => Math.max(0, value - 1))} disabled={frame === 0}>←</button>
        <input
          aria-label={`Select ${segment.replaceAll('_', ' ')} frame`}
          aria-valuetext={`Frame ${frame + 1} of ${info.frame_count}`}
          type="range"
          min={0}
          max={maxFrame}
          value={frame}
          onChange={(event) => setFrame(Number(event.target.value))}
          className="flex-1 accent-purple-500"
        />
        <button type="button" aria-label="Next frame" onClick={() => setFrame((value) => Math.min(maxFrame, value + 1))} disabled={frame === maxFrame}>→</button>
        <span aria-live="polite">{frame + 1} / {info.frame_count}</span>
      </div>
      <div className="aspect-[9/16] max-w-xs mx-auto bg-black rounded-xl overflow-hidden">
        {url
          ? <img src={url} alt={frameLabel} className="w-full h-full object-contain" />
          : status === 'loading-frame' ? <div role="status">Loading {frameLabel}…</div> : null}
      </div>
      {status === 'frame-error' && <p role="alert">{error}</p>}
    </section>
  )
}
