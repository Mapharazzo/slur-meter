import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { api, createIdempotencyKey } from '../../api'
import { useApp } from '../../context/AppContext'
import { usePollingResource } from '../../hooks/usePollingResource'
import PublishingPanel from '../publishing/PublishingPanel'
import ResourceState from '../shared/ResourceState'
import StatusBadge from '../shared/StatusBadge'
import SubtitleCandidates from '../subtitles/SubtitleCandidates'
import VideoPreview from '../video/VideoPreview'
import AttentionBanner from './AttentionBanner'
import DiagnosticsPanel from './DiagnosticsPanel'
import EventLog from './EventLog'
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

  const mutate = async (name, call, { announceGlobal = true } = {}) => {
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
      if (announceGlobal) setMutationError(message)
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
        retryDisabled={Boolean(pendingAction)}
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
            {snapshot.run.state === 'completed' ? (
              <details className="glass rounded-2xl p-5">
                <summary className="flex cursor-pointer flex-wrap items-center gap-2 font-semibold">
                  <span>Pipeline timeline</span>
                  <span className="text-sm font-normal opacity-70">· all stages complete · expand</span>
                </summary>
                <div className="mt-2">
                  <PipelineSteps stages={snapshot.stages} attempts={snapshot.attempts} availableActions={snapshot.available_actions} pendingAction={pendingAction} onRetry={retryStage} embedded />
                </div>
              </details>
            ) : (
              <PipelineSteps stages={snapshot.stages} attempts={snapshot.attempts} availableActions={snapshot.available_actions} pendingAction={pendingAction} onRetry={retryStage} />
            )}
            {(() => {
              const graph = snapshot.stages.find((stage) => stage.name === 'graph')
              const composite = snapshot.stages.find((stage) => stage.name === 'composite')
              const encode = snapshot.stages.find((stage) => stage.name === 'encode')
              const hasManifest = (stage) => (
                stage?.output_manifest
                && Object.keys(stage.output_manifest).length > 0
              )
              const previewAvailable = graph?.output_manifest?.details?.preview_file === 'preview.png'
              const videoAvailable = encode?.state === 'completed' && hasManifest(encode)
              const segmentTiming = composite?.output_manifest?.details?.timing || {}
              const compositeAvailable = (
                composite?.state === 'completed'
                && hasManifest(composite)
                && Object.keys(segmentTiming).length > 0
              )
              if (!previewAvailable && !videoAvailable && !compositeAvailable) return null
              return (
                <VideoPreview
                  jobId={snapshot.run.id}
                  previewAvailable={previewAvailable}
                  videoAvailable={videoAvailable}
                  compositeAvailable={compositeAvailable}
                  segmentTiming={segmentTiming}
                  client={client}
                />
              )
            })()}
            {(() => {
              const subtitleProps = {
                jobId, token: operatorToken, candidates: snapshot.candidates,
                availableActions: snapshot.available_actions, client,
                onRefresh: resource.refresh, actionRunner: mutate, pendingAction,
              }
              const needsOperator = snapshot.stages.some((stage) => (
                (stage.name === 'subtitle_selection' || stage.name === 'subtitle_discovery')
                && stage.state === 'needs_attention'
              ))
              if (needsOperator) return <SubtitleCandidates {...subtitleProps} />
              const chosen = snapshot.candidates.find((candidate) => (
                candidate.status === 'selected' || candidate.status === 'validated'
              ))
              const coverage = chosen?.coverage_percent
              const summary = chosen
                ? `using #${chosen.rank}${coverage != null ? ` · ${Math.round(coverage)}% coverage` : ''}`
                : snapshot.candidates.length
                  ? `${snapshot.candidates.length} discovered`
                  : 'none yet'
              return (
                <details className="glass rounded-2xl p-5">
                  <summary className="flex cursor-pointer flex-wrap items-center gap-2 font-semibold">
                    <span>Subtitle candidates</span>
                    <span className="text-sm font-normal opacity-70">· {summary} · expand to review or override</span>
                  </summary>
                  <div className="mt-4">
                    <SubtitleCandidates {...subtitleProps} embedded />
                  </div>
                </details>
              )
            })()}
            <PublishingPanel jobId={jobId} token={operatorToken} releases={snapshot.releases} publishingAttempts={snapshot.publishing_attempts} availableActions={snapshot.available_actions} client={client} onRefresh={resource.refresh} actionRunner={mutate} pendingAction={pendingAction} />
            <CostSummary costs={snapshot.costs} />
            <EventLog events={events} warning={eventWarning} />
            <DiagnosticsPanel detail={{ ...snapshot, events }} operatorToken={operatorToken} />
          </>
        )}
      </ResourceState>
    </div>
  )
}
