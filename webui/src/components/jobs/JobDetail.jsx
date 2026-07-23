import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api, createIdempotencyKey } from '../../api'
import { useApp } from '../../context/AppContext'
import { usePollingResource } from '../../hooks/usePollingResource'
import PublishingPanel from '../publishing/PublishingPanel'
import ResourceState from '../shared/ResourceState'
import StatusBadge from '../shared/StatusBadge'
import SubtitleCandidates from '../subtitles/SubtitleCandidates'
import AttentionBanner from './AttentionBanner'
import DiagnosticsPanel from './DiagnosticsPanel'
import PipelineSteps from './PipelineSteps'

const TERMINAL_STATES = new Set(['completed', 'failed', 'cancelled', 'needs_attention'])

function mergeEvents(current, incoming) {
  const byId = new Map(current.map((event) => [event.id, event]))
  incoming.forEach((event) => {
    if (!byId.has(event.id)) byId.set(event.id, event)
  })
  return [...byId.values()].sort((left, right) => left.id - right.id)
}

function CostSummary({ costs = [] }) {
  if (!costs.length) return null
  const total = costs.reduce((sum, cost) => sum + (Number(cost.amount_usd) || 0), 0)
  return (
    <section className="glass rounded-xl p-4" aria-labelledby="job-cost-heading">
      <h2 id="job-cost-heading">Job costs</h2>
      <p>${total.toFixed(2)} across {costs.length} persisted entr{costs.length === 1 ? 'y' : 'ies'}.</p>
    </section>
  )
}

export default function JobDetail({ client = api, pollingOptions = {} }) {
  const params = useParams()
  const jobId = params.jobId || params.imdbId
  const { operatorToken } = useApp()
  const loadDetail = useCallback(
    (signal) => client.getJob(jobId, { token: operatorToken, signal }),
    [client, jobId, operatorToken],
  )
  const { eventIntervalMs = 2_000, ...detailPollingOptions } = pollingOptions
  const resource = usePollingResource(loadDetail, {
    intervalMs: 2_000,
    staleAfterMs: 8_000,
    isTerminal: (snapshot) => TERMINAL_STATES.has(snapshot?.run?.state),
    dependencies: [jobId, operatorToken],
    ...detailPollingOptions,
  })
  const detail = resource.data
  const terminal = TERMINAL_STATES.has(detail?.run?.state)
  const [events, setEvents] = useState([])
  const eventCursor = useRef(0)
  const [eventWarning, setEventWarning] = useState('')
  const [pendingAction, setPendingAction] = useState(null)
  const pendingActionRef = useRef(null)
  const [mutationError, setMutationError] = useState('')

  useEffect(() => {
    setEvents([])
    eventCursor.current = 0
    setEventWarning('')
  }, [jobId])

  useEffect(() => {
    if (!detail) return
    setEvents((current) => mergeEvents(current, detail.events || []))
    eventCursor.current = Math.max(eventCursor.current, detail.last_event_id || 0)
  }, [detail])

  useEffect(() => {
    if (!detail || terminal) return undefined
    let active = true
    let timer = null
    let controller = null
    const pollEvents = async () => {
      if (!active || controller) return
      controller = new AbortController()
      try {
        const page = await client.listJobEvents(
          jobId,
          { after: eventCursor.current },
          { token: operatorToken, signal: controller.signal },
        )
        if (!active) return
        setEvents((current) => mergeEvents(current, page.items || []))
        eventCursor.current = Math.max(eventCursor.current, page.last_event_id || 0, ...(page.items || []).map((event) => event.id))
        setEventWarning('')
      } catch (failure) {
        if (active && !controller.signal.aborted) setEventWarning(failure?.message || 'Live events are temporarily unavailable.')
      } finally {
        controller = null
        if (active) timer = setTimeout(pollEvents, eventIntervalMs)
      }
    }
    pollEvents()
    return () => {
      active = false
      if (timer != null) clearTimeout(timer)
      controller?.abort()
    }
  }, [client, detail != null, eventIntervalMs, jobId, operatorToken, terminal])

  const mutate = async (name, call) => {
    if (pendingActionRef.current) return { ok: false, error: 'Another operator action is still in progress.' }
    pendingActionRef.current = name
    setPendingAction(name)
    setMutationError('')
    try {
      await call({ token: operatorToken, idempotencyKey: createIdempotencyKey() })
      await resource.refresh()
      return { ok: true }
    } catch (failure) {
      const message = failure?.message || 'The operator action failed.'
      setMutationError(message)
      return { ok: false, error: message }
    } finally {
      pendingActionRef.current = null
      setPendingAction(null)
    }
  }

  const retryStage = (stage) => mutate(`retry_stage:${stage.name}`, (options) => client.retryStage(jobId, stage.name, options))
  const bannerAction = (action) => mutate(action, (options) => (
    action === 'resume' ? client.resumeJob(jobId, options) : client.cancelJob(jobId, options)
  ))
  const manualRefresh = () => (
    pendingActionRef.current ? Promise.resolve(undefined) : resource.refresh()
  )

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <Link to="/jobs" className="text-sm">← All jobs</Link>
      <ResourceState
        resource={resource}
        loadingMessage="Loading job workspace…"
        emptyMessage="Run detail is missing."
        onRetry={manualRefresh}
        isEmpty={(value) => !value?.run}
      >
        {(snapshot) => (
          <>
            <header className="glass rounded-2xl p-5">
              <p className="eyebrow">Canonical run · {snapshot.run.id}</p>
              <div className="flex flex-wrap items-center gap-3">
                <h1 className="flex-1">{snapshot.run.label}</h1>
                <StatusBadge status={snapshot.run.state} />
                <button type="button" className="button" disabled={Boolean(pendingAction)} onClick={manualRefresh}>Refresh job</button>
              </div>
              <p>Current stage: {snapshot.run.current_stage || 'None'} · Updated <time dateTime={snapshot.run.updated_at}>{snapshot.run.updated_at}</time></p>
            </header>

            <AttentionBanner run={snapshot.run} availableActions={snapshot.available_actions} pendingAction={pendingAction} onAction={bannerAction} />
            {mutationError && <p role="alert" className="inline-error">{mutationError}</p>}
            <PipelineSteps stages={snapshot.stages} attempts={snapshot.attempts} availableActions={snapshot.available_actions} pendingAction={pendingAction} onRetry={retryStage} />
            <SubtitleCandidates jobId={jobId} token={operatorToken} candidates={snapshot.candidates} availableActions={snapshot.available_actions} client={client} onRefresh={resource.refresh} actionRunner={mutate} pendingAction={pendingAction} />
            <PublishingPanel jobId={jobId} token={operatorToken} releases={snapshot.releases} publishingAttempts={snapshot.publishing_attempts} availableActions={snapshot.available_actions} client={client} onRefresh={resource.refresh} actionRunner={mutate} pendingAction={pendingAction} />
            <CostSummary costs={snapshot.costs} />
            <section aria-labelledby="event-stream-heading" className="glass rounded-xl p-4">
              <h2 id="event-stream-heading">Operational events</h2>
              {eventWarning && <p role="alert">{eventWarning} Showing persisted events.</p>}
              {events.length ? <ol>{events.map((event) => <li key={event.id}><time dateTime={event.created_at}>{event.created_at}</time> · {event.message}</li>)}</ol> : <p>No events have been persisted.</p>}
            </section>
            <DiagnosticsPanel detail={{ ...snapshot, events }} operatorToken={operatorToken} />
          </>
        )}
      </ResourceState>
    </div>
  )
}
