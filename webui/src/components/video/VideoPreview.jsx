import { useState } from 'react'
import { api } from '../../api'
import SegmentPlayer from './SegmentPlayer'
import FrameBrowser from './FrameBrowser'

const SEGMENTS = ['intro_hold', 'intro_transition', 'graph', 'verdict']

export default function VideoPreview({ imdbId, segmentTiming }) {
  const [activeTab, setActiveTab] = useState('full')
  const [activeSegment, setActiveSegment] = useState(null)

  return (
    <div className="glass rounded-2xl p-6 space-y-4">
      <h3 className="text-sm font-bold text-gray-400 uppercase tracking-wider">Video Preview</h3>

      {/* Tabs */}
      <div className="flex gap-2 flex-wrap">
        <button
          onClick={() => { setActiveTab('full'); setActiveSegment(null) }}
          className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-all ${
            activeTab === 'full' ? 'bg-white/10 text-white' : 'text-gray-500 hover:text-white'
          }`}
        >
          Full Video
        </button>
        {SEGMENTS.map(seg => {
          const timing = segmentTiming?.[seg]
          return (
            <button
              key={seg}
              onClick={() => { setActiveTab('segment'); setActiveSegment(seg) }}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-all ${
                activeTab === 'segment' && activeSegment === seg
                  ? 'bg-white/10 text-white'
                  : 'text-gray-500 hover:text-white'
              }`}
            >
              {seg.replace('_', ' ')}
              {timing && (
                <span className="ml-1 text-gray-700">
                  ({(timing.end_time - timing.start_time).toFixed(1)}s)
                </span>
              )}
            </button>
          )
        })}
      </div>

      {/* Content */}
      {activeTab === 'full' && (
        <div className="aspect-[9/16] max-w-xs mx-auto bg-black rounded-xl overflow-hidden">
          <video
            src={api.videoUrl(imdbId)}
            controls
            className="w-full h-full object-contain"
          />
        </div>
      )}

      {activeTab === 'segment' && activeSegment && (
        <div className="space-y-4">
          <SegmentPlayer imdbId={imdbId} segment={activeSegment} />
          <FrameBrowser imdbId={imdbId} segment={activeSegment} />
        </div>
      )}

      {/* Download */}
      <div className="text-center">
        <a
          href={api.videoUrl(imdbId)}
          download
          className="inline-block px-6 py-2.5 bg-gradient-to-r from-red-500 to-purple-600 rounded-xl text-sm font-bold hover:opacity-90 transition-all"
        >
          Download MP4
        </a>
      </div>
    </div>
  )
}
