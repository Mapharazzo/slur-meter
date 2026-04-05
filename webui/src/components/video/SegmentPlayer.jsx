import { useState, useEffect, useRef, useCallback } from 'react'
import { api } from '../../api'

export default function SegmentPlayer({ imdbId, segment }) {
  const [info, setInfo] = useState(null)
  const [playing, setPlaying] = useState(false)
  const [currentFrame, setCurrentFrame] = useState(0)
  const canvasRef = useRef(null)
  const animRef = useRef(null)
  const imgCache = useRef({})

  useEffect(() => {
    api.getSegmentInfo(imdbId, segment)
      .then(setInfo)
      .catch(() => setInfo(null))
    return () => {
      setPlaying(false)
      if (animRef.current) cancelAnimationFrame(animRef.current)
    }
  }, [imdbId, segment])

  const preloadFrame = useCallback((frame) => {
    const url = api.frameUrl(imdbId, segment, frame)
    if (!imgCache.current[frame]) {
      const img = new Image()
      img.src = url
      imgCache.current[frame] = img
    }
    return imgCache.current[frame]
  }, [imdbId, segment])

  const drawFrame = useCallback((frame) => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    const img = preloadFrame(frame)
    if (img.complete) {
      canvas.width = img.naturalWidth || 540
      canvas.height = img.naturalHeight || 960
      ctx.drawImage(img, 0, 0)
    } else {
      img.onload = () => {
        canvas.width = img.naturalWidth
        canvas.height = img.naturalHeight
        ctx.drawImage(img, 0, 0)
      }
    }
  }, [preloadFrame])

  useEffect(() => {
    drawFrame(currentFrame)
    // Preload next frames
    for (let i = 1; i <= 5; i++) {
      if (info && currentFrame + i < info.frame_count) {
        preloadFrame(currentFrame + i)
      }
    }
  }, [currentFrame, drawFrame, preloadFrame, info])

  useEffect(() => {
    if (!playing || !info) return
    let frame = currentFrame
    const fps = 30
    const interval = 1000 / fps
    let lastTime = performance.now()

    const animate = (now) => {
      if (now - lastTime >= interval) {
        frame = (frame + 1) % info.frame_count
        setCurrentFrame(frame)
        lastTime = now
      }
      animRef.current = requestAnimationFrame(animate)
    }
    animRef.current = requestAnimationFrame(animate)
    return () => { if (animRef.current) cancelAnimationFrame(animRef.current) }
  }, [playing, info])

  if (!info) return <div className="text-gray-600 text-sm">Loading segment...</div>

  return (
    <div className="space-y-3">
      <div className="aspect-[9/16] max-w-xs mx-auto bg-black rounded-xl overflow-hidden">
        <canvas ref={canvasRef} className="w-full h-full object-contain" />
      </div>
      <div className="flex items-center justify-center gap-3">
        <button
          onClick={() => setPlaying(!playing)}
          className="px-4 py-1.5 rounded-lg bg-white/10 text-sm font-medium hover:bg-white/20 transition-all"
        >
          {playing ? 'Pause' : 'Play'}
        </button>
        <span className="text-xs text-gray-500 font-mono">
          {currentFrame} / {info.frame_count}
        </span>
      </div>
    </div>
  )
}
