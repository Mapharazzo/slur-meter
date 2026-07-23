import { useEffect, useRef, useState } from 'react'

import { api } from '../../api'
import { useApp } from '../../context/AppContext'
import FrameBrowser from './FrameBrowser'
import SegmentPlayer from './SegmentPlayer'

const SEGMENTS = ['intro_hold', 'intro_transition', 'graph', 'verdict']

function useMediaBlob(load, enabled, dependencies) {
  const [state, setState] = useState({ status: enabled ? 'loading' : 'unavailable', url: '', error: '' })
  const currentUrl = useRef('')
  useEffect(() => {
    if (currentUrl.current) URL.revokeObjectURL(currentUrl.current)
    currentUrl.current = ''
    if (!enabled) {
      setState({ status: 'unavailable', url: '', error: '' })
      return undefined
    }
    const controller = new AbortController()
    let active = true
    setState({ status: 'loading', url: '', error: '' })
    load(controller.signal).then((blob) => {
      if (!active || controller.signal.aborted) return
      const url = URL.createObjectURL(blob)
      currentUrl.current = url
      setState({ status: 'success', url, error: '' })
    }).catch((failure) => {
      if (!active || controller.signal.aborted) return
      setState({ status: 'error', url: '', error: failure?.message || 'The media could not be loaded.' })
    })
    return () => {
      active = false
      controller.abort()
      if (currentUrl.current) URL.revokeObjectURL(currentUrl.current)
      currentUrl.current = ''
    }
  // The caller supplies an explicit media identity.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, ...dependencies])
  return state
}

export default function VideoPreview({
  jobId: jobIdProp,
  imdbId,
  segmentTiming = {},
  previewAvailable = false,
  videoAvailable = false,
  compositeAvailable = false,
  client = api,
}) {
  const jobId = jobIdProp ?? imdbId
  const { operatorToken } = useApp()
  const availableSegments = SEGMENTS.filter((name) => segmentTiming?.[name])
  const segmentIdentity = availableSegments.join('|')
  const initialTab = previewAvailable ? 'preview' : videoAvailable ? 'video' : compositeAvailable ? 'segment' : 'unavailable'
  const [activeTab, setActiveTab] = useState(initialTab)
  const [activeSegment, setActiveSegment] = useState(() => availableSegments[0] || 'graph')
  useEffect(() => {
    setActiveTab(previewAvailable ? 'preview' : videoAvailable ? 'video' : compositeAvailable ? 'segment' : 'unavailable')
    setActiveSegment(availableSegments[0] || 'graph')
  // Equivalent polling snapshots retain the operator's selected media.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [compositeAvailable, jobId, previewAvailable, segmentIdentity, videoAvailable])
  const preview = useMediaBlob(
    (signal) => client.fetchPreview(jobId, { token: operatorToken, signal }),
    previewAvailable,
    [client, jobId, operatorToken],
  )
  const video = useMediaBlob(
    (signal) => client.fetchVideo(jobId, { token: operatorToken, signal }),
    videoAvailable && activeTab === 'video',
    [client, jobId, operatorToken, activeTab],
  )

  const downloadVideo = () => {
    if (!video.url) return
    const link = document.createElement('a')
    link.href = video.url
    link.download = `slur-meter-${jobId}.mp4`
    link.click()
  }

  return (
    <section aria-labelledby="video-preview-heading" className="glass rounded-2xl p-6 space-y-4">
      <h2 id="video-preview-heading">Media preview</h2>
      <div role="group" aria-label="Media preview choices" className="flex gap-2 flex-wrap">
        {previewAvailable && <button type="button" aria-pressed={activeTab === 'preview'} onClick={() => setActiveTab('preview')}>Graph preview</button>}
        {videoAvailable && <button type="button" aria-pressed={activeTab === 'video'} onClick={() => setActiveTab('video')}>Final video</button>}
        {compositeAvailable && availableSegments.map((name) => (
          <button
            type="button"
            key={name}
            aria-pressed={activeTab === 'segment' && activeSegment === name}
            onClick={() => { setActiveTab('segment'); setActiveSegment(name) }}
          >
            {name.replaceAll('_', ' ')}
          </button>
        ))}
      </div>

      {activeTab === 'preview' && (
        preview.status === 'loading' ? <div role="status">Loading graph preview…</div>
          : preview.status === 'error' ? <div role="alert">{preview.error}</div>
            : <img src={preview.url} alt="Latest graph preview" className="max-w-xs" />
      )}
      {activeTab === 'video' && (
        video.status === 'loading' ? <div role="status">Loading final video…</div>
          : video.status === 'error' ? <div role="alert">{video.error}</div>
            : <>
              <video src={video.url} controls aria-label={`Final video for ${jobId}`} className="max-w-xs" />
              <button type="button" onClick={downloadVideo} disabled={!video.url}>Download final MP4</button>
            </>
      )}
      {activeTab === 'segment' && activeSegment && (
        <div className="space-y-4">
          <SegmentPlayer jobId={jobId} segment={activeSegment} client={client} />
          <FrameBrowser jobId={jobId} segment={activeSegment} client={client} />
        </div>
      )}
      {activeTab === 'unavailable' && <p>No durable media artifact is available yet.</p>}
    </section>
  )
}
