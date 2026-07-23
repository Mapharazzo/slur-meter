import { useEffect, useRef, useState } from 'react'

import { api } from '../../api'
import { useApp } from '../../context/AppContext'

// Lazily fetch a completed job's graph preview as an authenticated blob and
// show it as a card thumbnail. Falls back to a placeholder tile on any error.
export default function CompletedThumb({ jobId, client = api }) {
  const { operatorToken } = useApp()
  const [url, setUrl] = useState('')
  const current = useRef('')

  useEffect(() => {
    if (typeof client.fetchPreview !== 'function') return undefined
    let active = true
    const controller = new AbortController()
    client
      .fetchPreview(jobId, { token: operatorToken, signal: controller.signal })
      .then((blob) => {
        if (!active) return
        const objectUrl = URL.createObjectURL(blob)
        current.current = objectUrl
        setUrl(objectUrl)
      })
      .catch(() => {})
    return () => {
      active = false
      controller.abort()
      if (current.current) URL.revokeObjectURL(current.current)
      current.current = ''
      setUrl('')
    }
  }, [client, jobId, operatorToken])

  return (
    <div className="flex h-40 w-full items-center justify-center overflow-hidden bg-black/40">
      {url
        ? <img src={url} alt="" className="h-full w-full object-cover" />
        : <span aria-hidden="true" className="text-3xl opacity-40">🎬</span>}
    </div>
  )
}
