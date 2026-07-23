import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { api } from '../../api'
import { useApp } from '../../context/AppContext'
import { usePollingResource } from '../../hooks/usePollingResource'
import ResourceState from '../shared/ResourceState'
import JobList from '../jobs/JobList'
import JobSubmit from '../jobs/JobSubmit'
import StatsGrid from './StatsGrid'

const PAGE_SIZE = 20
const MAX_PAGE = 50_000
const FILTER_STATES = ['running', 'needs_attention', 'failed', 'queued', 'cancelled', 'completed']

function normalizedUrlState(params) {
  const query = (params.get('query') || '').trim().slice(0, 200)
  const candidateState = params.get('state') || ''
  const state = FILTER_STATES.includes(candidateState) ? candidateState : ''
  const rawPage = params.get('page')
  const numericPage = /^\d+$/.test(rawPage || '') ? Number(rawPage) : 1
  const page = Math.min(MAX_PAGE, Math.max(1, Number.isSafeInteger(numericPage) ? numericPage : 1))
  return { query, state, page }
}

function paramsFor({ query, state, page }) {
  const next = new URLSearchParams()
  if (query) next.set('query', query)
  if (state) next.set('state', state)
  if (page > 1) next.set('page', String(page))
  return next
}

export default function OperationsOverview({ client = api, poll = true, pollingOptions = {} }) {
  const { operatorToken } = useApp()
  const [searchParams, setSearchParams] = useSearchParams()
  const urlState = useMemo(() => normalizedUrlState(searchParams), [searchParams])
  const canonicalSearch = paramsFor(urlState).toString()
  const [draftQuery, setDraftQuery] = useState(urlState.query)
  const [draftState, setDraftState] = useState(urlState.state)
  const queueRequestKey = useMemo(
    () => JSON.stringify([urlState.query, urlState.state, urlState.page]),
    [urlState.page, urlState.query, urlState.state],
  )

  useEffect(() => {
    if (searchParams.toString() !== canonicalSearch) {
      setSearchParams(canonicalSearch, { replace: true })
    }
  }, [canonicalSearch, searchParams, setSearchParams])

  useEffect(() => {
    setDraftQuery(urlState.query)
    setDraftState(urlState.state)
  }, [urlState.query, urlState.state])

  const loadSummary = useCallback(
    (signal) => client.operationsSummary({ token: operatorToken, signal }),
    [client, operatorToken],
  )
  const loadJobs = useCallback(
    async (signal) => {
      const params = {
        ...(urlState.query ? { query: urlState.query } : {}),
        ...(urlState.state ? { state: urlState.state } : {}),
        limit: PAGE_SIZE,
        offset: (urlState.page - 1) * PAGE_SIZE,
      }
      const page = await client.listJobs(params, { token: operatorToken, signal })
      return { ...page, __requestKey: queueRequestKey }
    },
    [client, operatorToken, queueRequestKey, urlState.page, urlState.query, urlState.state],
  )
  const polling = {
    intervalMs: poll ? 5_000 : 2_147_483_647,
    staleAfterMs: 15_000,
    ...pollingOptions,
  }
  const summary = usePollingResource(loadSummary, {
    ...polling,
    dependencies: [operatorToken],
  })
  const jobs = usePollingResource(loadJobs, {
    ...polling,
    dependencies: [operatorToken, urlState.query, urlState.state, urlState.page],
  })
  const pageCount = jobs.data ? Math.max(1, Math.ceil(jobs.data.total / PAGE_SIZE)) : 1

  useEffect(() => {
    if (
      !jobs.hasData
      || !jobs.data
      || jobs.data.__requestKey !== queueRequestKey
      || urlState.page <= pageCount
    ) return
    setSearchParams(paramsFor({ ...urlState, page: pageCount }), { replace: true })
  }, [jobs.data, jobs.hasData, pageCount, queueRequestKey, setSearchParams, urlState])

  const applyFilters = (event) => {
    event.preventDefault()
    setSearchParams(paramsFor({ query: draftQuery.trim().slice(0, 200), state: draftState, page: 1 }))
  }
  const currentPage = Math.min(urlState.page, pageCount)
  const changePage = (nextPage) => {
    setSearchParams(paramsFor({ ...urlState, page: Math.max(1, Math.min(pageCount, nextPage)) }))
  }

  return (
    <div className="operations-page">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Operational truth</p>
          <h1>Operations queue</h1>
          <p>Follow every run from durable submission to a completed or actionable outcome.</p>
        </div>
        <button type="button" className="button button--quiet" onClick={() => { summary.refresh(); jobs.refresh() }}>
          Refresh queue
        </button>
      </div>

      <ResourceState
        resource={summary}
        isEmpty={() => false}
        loadingMessage="Loading operations summary…"
      >
        {(data) => <StatsGrid summary={data} />}
      </ResourceState>

      <JobSubmit client={client} />

      <section className="queue-panel" aria-labelledby="queue-view-heading">
        <div className="queue-toolbar">
          <div>
            <p className="eyebrow">Primary queue</p>
            <h2 id="queue-view-heading" className="sr-only">Queue filters and results</h2>
          </div>
          <form role="search" onSubmit={applyFilters} className="queue-filters">
            <label className="field-label">
              <span>Search runs</span>
              <input type="search" value={draftQuery} onChange={(event) => setDraftQuery(event.target.value)} />
            </label>
            <label className="field-label">
              <span>State filter</span>
              <select value={draftState} onChange={(event) => setDraftState(event.target.value)}>
                <option value="">All durable states</option>
                {FILTER_STATES.map((state) => <option key={state} value={state}>{state.replaceAll('_', ' ')}</option>)}
              </select>
            </label>
            <button type="submit" className="button button--secondary">Apply filters</button>
          </form>
        </div>

        <ResourceState
          resource={jobs}
          isEmpty={(data) => data?.total === 0}
          emptyMessage="No runs match this view."
          loadingMessage="Loading operations queue…"
        >
          {(data) => (
            <>
              <p className="page-count" aria-live="polite">Showing {data.items.length} of {data.total} matching runs.</p>
              <JobList jobs={data.items} />
              <nav className="pagination" aria-label="Queue pages">
                <button type="button" className="button button--quiet" onClick={() => changePage(currentPage - 1)} disabled={currentPage <= 1}>Previous page</button>
                <span>Page {currentPage} of {pageCount}</span>
                <button type="button" className="button button--quiet" onClick={() => changePage(currentPage + 1)} disabled={currentPage >= pageCount}>Next page</button>
              </nav>
            </>
          )}
        </ResourceState>
      </section>
    </div>
  )
}
