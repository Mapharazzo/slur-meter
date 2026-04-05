import { useState, useEffect } from 'react'
import { api } from '../../api'

export default function FrameBrowser({ imdbId, segment }) {
  const [info, setInfo] = useState(null)
  const [frame, setFrame] = useState(0)

  useEffect(() => {
    api.getSegmentInfo(imdbId, segment)
      .then(data => { setInfo(data); setFrame(0) })
      .catch(() => setInfo(null))
  }, [imdbId, segment])

  if (!info || info.frame_count === 0) return null

  const maxFrame = info.frame_count - 1

  return (
    <div className="glass rounded-xl p-4 space-y-3">
      <h4 className="text-xs font-bold text-gray-500 uppercase tracking-wider">Frame Browser</h4>

      <div className="flex items-center gap-3">
        <button
          onClick={() => setFrame(Math.max(0, frame - 1))}
          disabled={frame === 0}
          className="px-2 py-1 rounded bg-white/10 text-sm disabled:opacity-30 hover:bg-white/20 transition-all"
        >
          &larr;
        </button>

        <input
          type="range"
          min={0}
          max={maxFrame}
          value={frame}
          onChange={e => setFrame(Number(e.target.value))}
          className="flex-1 accent-purple-500"
        />

        <button
          onClick={() => setFrame(Math.min(maxFrame, frame + 1))}
          disabled={frame === maxFrame}
          className="px-2 py-1 rounded bg-white/10 text-sm disabled:opacity-30 hover:bg-white/20 transition-all"
        >
          &rarr;
        </button>

        <span className="text-xs text-gray-600 font-mono w-20 text-right">
          {frame} / {maxFrame}
        </span>
      </div>

      <div className="aspect-[9/16] max-w-xs mx-auto bg-black rounded-xl overflow-hidden">
        <img
          src={api.frameUrl(imdbId, segment, frame)}
          alt={`${segment} frame ${frame}`}
          className="w-full h-full object-contain"
        />
      </div>
    </div>
  )
}
