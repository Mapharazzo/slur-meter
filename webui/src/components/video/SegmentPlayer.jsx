import { useCallback, useEffect, useRef, useState } from 'react'

import { api } from '../../api'
import { useApp } from '../../context/AppContext'

export default function SegmentPlayer({ jobId: jobIdProp, imdbId, segment, client = api }) {
  const jobId = jobIdProp ?? imdbId
  const { operatorToken } = useApp()
  const [info, setInfo] = useState(null)
  const [infoStatus, setInfoStatus] = useState('loading')
  const [error, setError] = useState('')
  const [playing, setPlaying] = useState(false)
  const [currentFrame, setCurrentFrame] = useState(0)
  const [frameUrl, setFrameUrl] = useState('')
  const [frameLoading, setFrameLoading] = useState(false)
  const generation = useRef(0)
  const frameController = useRef(null)
  const currentUrl = useRef('')
  const animation = useRef(null)
  const frameLoadingRef = useRef(false)

  const revokeCurrentUrl = useCallback(() => {
    if (currentUrl.current) URL.revokeObjectURL(currentUrl.current)
    currentUrl.current = ''
    setFrameUrl('')
  }, [])

  useEffect(() => {
    const ownGeneration = ++generation.current
    const controller = new AbortController()
    frameController.current?.abort()
    frameController.current = null
    if (animation.current != null) cancelAnimationFrame(animation.current)
    animation.current = null
    revokeCurrentUrl()
    setInfo(null)
    setInfoStatus('loading')
    setError('')
    setPlaying(false)
    setCurrentFrame(0)
    setFrameLoading(false)
    frameLoadingRef.current = false

    client.getSegmentInfo(jobId, segment, { token: operatorToken, signal: controller.signal })
      .then((value) => {
        if (generation.current !== ownGeneration || controller.signal.aborted) return
        setInfo(value)
        setInfoStatus('success')
      })
      .catch((failure) => {
        if (generation.current !== ownGeneration || controller.signal.aborted) return
        setInfoStatus('error')
        setError(failure?.message || 'The segment could not be loaded.')
      })

    return () => {
      controller.abort()
      generation.current += 1
      frameController.current?.abort()
      frameController.current = null
      if (animation.current != null) cancelAnimationFrame(animation.current)
      animation.current = null
      revokeCurrentUrl()
    }
  }, [client, jobId, operatorToken, revokeCurrentUrl, segment])

  useEffect(() => {
    if (!info || info.frame_count <= 0) return undefined
    const ownGeneration = generation.current
    const controller = new AbortController()
    frameController.current?.abort()
    frameController.current = controller
    revokeCurrentUrl()
    setError('')
    setFrameLoading(true)
    frameLoadingRef.current = true
    client.fetchFrame(jobId, segment, currentFrame, {
      token: operatorToken,
      signal: controller.signal,
    }).then((blob) => {
      if (generation.current !== ownGeneration || controller.signal.aborted) return
      const url = URL.createObjectURL(blob)
      currentUrl.current = url
      setFrameUrl(url)
    }).catch((failure) => {
      if (generation.current !== ownGeneration || controller.signal.aborted) return
      setError(failure?.message || 'The frame could not be loaded.')
    }).finally(() => {
      if (generation.current !== ownGeneration || controller.signal.aborted) return
      frameLoadingRef.current = false
      setFrameLoading(false)
    })
    return () => controller.abort()
  }, [client, currentFrame, info, jobId, operatorToken, revokeCurrentUrl, segment])

  useEffect(() => {
    if (!playing || !info?.frame_count || !(Number(info.fps) > 0)) return undefined
    const interval = 1000 / Number(info.fps)
    let lastTime = null
    const animate = (now) => {
      if (lastTime == null) lastTime = now
      else if (!frameLoadingRef.current && now - lastTime >= interval) {
        setCurrentFrame((frame) => (frame + 1) % info.frame_count)
        lastTime = now
      }
      animation.current = requestAnimationFrame(animate)
    }
    animation.current = requestAnimationFrame(animate)
    return () => {
      if (animation.current != null) cancelAnimationFrame(animation.current)
      animation.current = null
    }
  }, [info, playing])

  useEffect(() => {
    const stopWhenHidden = () => {
      if (document.visibilityState === 'hidden') setPlaying(false)
    }
    document.addEventListener('visibilitychange', stopWhenHidden)
    return () => document.removeEventListener('visibilitychange', stopWhenHidden)
  }, [])

  if (infoStatus === 'loading') {
    return <div role="status">Loading {segment.replaceAll('_', ' ')} segment…</div>
  }
  if (infoStatus === 'error') return <div role="alert">{error}</div>
  if (!info?.frame_count) return <p>This segment has no rendered frames.</p>

  const label = `${segment.replaceAll('_', ' ')} frame ${currentFrame + 1} of ${info.frame_count}`
  return (
    <section aria-label={`${segment.replaceAll('_', ' ')} segment player`} className="space-y-3">
      <div className="aspect-[9/16] max-w-xs mx-auto bg-black rounded-xl overflow-hidden">
        {frameUrl
          ? <img src={frameUrl} alt={label} className="w-full h-full object-contain" />
          : frameLoading
            ? <div role="status">Loading {label}…</div>
            : null}
      </div>
      {error && <p role="alert">{error}</p>}
      <div className="flex items-center justify-center gap-3">
        <button
          type="button"
          onClick={() => setPlaying((value) => !value)}
          disabled={Boolean(error)}
          aria-label={`${playing ? 'Pause' : 'Play'} ${segment.replaceAll('_', ' ')} segment`}
          className="button"
        >
          {playing ? 'Pause' : 'Play'}
        </button>
        <span aria-live="polite" className="text-xs text-gray-500 font-mono">
          {currentFrame + 1} / {info.frame_count}
        </span>
      </div>
    </section>
  )
}
