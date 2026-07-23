const API_BASE = '/api'
const DEFAULT_TIMEOUT_MS = 15_000
const JSON_CONTENT_TYPE = /(?:application\/json|\+json)(?:\s*;|$)/i
const INVALID_HEADER_VALUE = /[\u0000-\u001f\u007f]/

export class ApiError extends Error {
  constructor(message, {
    status = null,
    code = 'request_failed',
    retryable = false,
    details = null,
    requestId = null,
  } = {}) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.retryable = retryable
    this.details = details
    this.requestId = requestId
  }
}

function invalidRequest(message = 'The API request is invalid.') {
  return new ApiError(message, { code: 'invalid_request', retryable: false })
}

function containsDotSegment(path) {
  const pathname = path.split('?', 1)[0]
  return pathname.split('/').some((rawSegment) => {
    let decoded = rawSegment
    try {
      for (let pass = 0; pass < 2; pass += 1) {
        const next = decodeURIComponent(decoded)
        if (next === decoded) break
        decoded = next
      }
    } catch {
      return true
    }
    return decoded.split(/[\\/]/).some((part) => part === '.' || part === '..')
  })
}

function apiPath(path, query) {
  if (typeof path !== 'string' || !path || path.includes('#')) {
    throw invalidRequest()
  }
  if (/^[a-z][a-z\d+.-]*:/i.test(path) || path.startsWith('//')) {
    throw invalidRequest('Cross-origin API requests are not allowed.')
  }
  if (containsDotSegment(path)) {
    throw invalidRequest('API path traversal is not allowed.')
  }

  let url
  if (path === API_BASE || path.startsWith(`${API_BASE}/`)) {
    url = path
  } else {
    url = `${API_BASE}/${path.replace(/^\/+/, '')}`
  }

  if (query) {
    const separator = url.includes('?') ? '&' : '?'
    const params = new URLSearchParams()
    Object.entries(query).forEach(([key, value]) => {
      if (value == null) return
      if (Array.isArray(value)) {
        value.forEach((item) => {
          if (item != null) params.append(key, String(item))
        })
      } else {
        params.append(key, String(value))
      }
    })
    const encoded = params.toString()
    if (encoded) url += `${separator}${encoded}`
  }
  return url
}

function isJson(response) {
  return JSON_CONTENT_TYPE.test(response.headers.get('Content-Type') || '')
}

function invalidResponse(status = null) {
  return new ApiError('The server returned an invalid response.', {
    status,
    code: 'invalid_response',
    retryable: status == null || status >= 500,
  })
}

function redact(value, secret) {
  if (!secret) return value
  if (typeof value === 'string') return value.split(secret).join('[redacted]')
  if (Array.isArray(value)) return value.map((item) => redact(item, secret))
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [
      redact(key, secret),
      redact(item, secret),
    ]))
  }
  return value
}

function structuredError(payload, status, secret) {
  const error = payload?.error
  if (
    !error
    || typeof error !== 'object'
    || typeof error.code !== 'string'
    || !error.code.trim()
    || typeof error.message !== 'string'
    || !error.message.trim()
    || typeof error.retryable !== 'boolean'
    || !error.details
    || typeof error.details !== 'object'
    || Array.isArray(error.details)
    || typeof error.request_id !== 'string'
    || !error.request_id.trim()
  ) {
    return null
  }
  return new ApiError(redact(error.message, secret), {
    status,
    code: redact(error.code, secret),
    retryable: error.retryable,
    details: redact(error.details ?? null, secret),
    requestId: redact(error.request_id ?? null, secret),
  })
}

/**
 * Fetches one same-application API resource.
 *
 * `path` may be a complete `/api/...` path or a relative path joined below
 * `/api`. `json`, `token`, `idempotencyKey`, `query`, and `timeoutMs` are
 * transport options and are not forwarded to fetch.
 */
export async function request(path, options = {}) {
  const {
    token,
    timeoutMs = DEFAULT_TIMEOUT_MS,
    json,
    query,
    idempotencyKey,
    responseType = 'json',
    signal: callerSignal,
    headers: callerHeaders,
    method = 'GET',
    ...fetchOptions
  } = options
  const url = apiPath(path, query)
  if (!Number.isFinite(timeoutMs) || timeoutMs < 0) throw invalidRequest()
  if (!['json', 'blob'].includes(responseType)) throw invalidRequest()
  if (json !== undefined && fetchOptions.body !== undefined) throw invalidRequest()

  let headers
  try {
    headers = new Headers(callerHeaders)
  } catch {
    throw invalidRequest('The request headers are invalid.')
  }
  const normalizedToken = typeof token === 'string' ? token.trim() : ''
  if (INVALID_HEADER_VALUE.test(normalizedToken)) {
    throw invalidRequest('The operator token is invalid.')
  }
  if (normalizedToken) headers.set('Authorization', `Bearer ${normalizedToken}`)
  if (idempotencyKey != null) {
    const normalizedKey = String(idempotencyKey).trim()
    if (!normalizedKey || INVALID_HEADER_VALUE.test(normalizedKey)) {
      throw invalidRequest('The idempotency key is invalid.')
    }
    headers.set('Idempotency-Key', normalizedKey)
  }
  if (json !== undefined) {
    if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
    fetchOptions.body = JSON.stringify(json)
  }

  const controller = new AbortController()
  let timedOut = false
  const forwardAbort = () => controller.abort(callerSignal?.reason)
  if (callerSignal) {
    callerSignal.addEventListener('abort', forwardAbort, { once: true })
    if (callerSignal.aborted) forwardAbort()
  }
  const timeoutId = timeoutMs > 0
    ? setTimeout(() => {
        timedOut = true
        controller.abort(new DOMException('Request timed out', 'TimeoutError'))
      }, timeoutMs)
    : null
  let rejectForAbort
  const aborted = new Promise((_resolve, reject) => {
    rejectForAbort = reject
  })
  const stopOnInternalAbort = () => {
    rejectForAbort(new DOMException('Request aborted', 'AbortError'))
  }
  controller.signal.addEventListener('abort', stopOnInternalAbort, { once: true })
  if (controller.signal.aborted) stopOnInternalAbort()

  const performRequest = async () => {
    const response = await fetch(url, {
      ...fetchOptions,
      method: String(method).toUpperCase(),
      headers,
      signal: controller.signal,
    })

    if (response.status === 204 || response.status === 205) return null
    if (responseType === 'blob' && response.ok) return response.blob()
    if (!isJson(response)) throw invalidResponse(response.status)

    let payload
    try {
      payload = await response.json()
    } catch {
      throw invalidResponse(response.status)
    }
    if (!response.ok) {
      throw structuredError(payload, response.status, normalizedToken) || invalidResponse(response.status)
    }
    return payload
  }

  try {
    return await Promise.race([performRequest(), aborted])
  } catch (error) {
    if (error instanceof ApiError) throw error
    if (timedOut) {
      throw new ApiError('The request timed out.', { code: 'timeout', retryable: true })
    }
    if (callerSignal?.aborted) {
      throw new ApiError('The request was cancelled.', { code: 'aborted', retryable: false })
    }
    if (error?.name === 'AbortError') {
      throw new ApiError('The request was cancelled.', { code: 'aborted', retryable: false })
    }
    throw new ApiError('The server could not be reached.', {
      code: 'network_error',
      retryable: true,
    })
  } finally {
    if (timeoutId != null) clearTimeout(timeoutId)
    controller.signal.removeEventListener('abort', stopOnInternalAbort)
    if (callerSignal) callerSignal.removeEventListener('abort', forwardAbort)
  }
}

function segment(value) {
  return encodeURIComponent(String(value))
}

function mutation(path, options = {}, body) {
  return request(path, {
    ...options,
    method: 'POST',
    ...(body === undefined ? {} : { json: body }),
  })
}

function reconciliationBody(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    throw invalidRequest('The publishing reconciliation is invalid.')
  }
  if (body.reconciliation === 'not_uploaded') {
    if (body.remote_id != null) throw invalidRequest('The publishing reconciliation is invalid.')
    return { reconciliation: 'not_uploaded' }
  }
  if (body.reconciliation !== 'uploaded' || typeof body.remote_id !== 'string') {
    throw invalidRequest('The publishing reconciliation is invalid.')
  }
  const remoteId = body.remote_id.trim()
  if (!remoteId || INVALID_HEADER_VALUE.test(remoteId) || !/^[\p{L}\p{N}._:-]+$/u.test(remoteId)) {
    throw invalidRequest('The publishing reconciliation is invalid.')
  }
  return { reconciliation: 'uploaded', remote_id: remoteId }
}

export function createIdempotencyKey() {
  if (typeof crypto?.randomUUID === 'function') return `ui_${crypto.randomUUID()}`
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  const encoded = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')
  return `ui_${encoded}`
}

export const api = {
  health: (options = {}) => request('/health', options),
  operationsSummary: (options = {}) => request('/operations/summary', options),
  listJobs: (params = {}, options = {}) => request('/jobs', { ...options, query: params }),
  getJob: (id, options = {}) => request(`/jobs/${segment(id)}`, options),
  listJobEvents: (id, params = {}, options = {}) => request(`/jobs/${segment(id)}/events`, { ...options, query: params }),
  getJobSteps: (id, options = {}) => request(`/jobs/${segment(id)}`, options),
  getJobCosts: (id, options = {}) => request(`/jobs/${segment(id)}/costs`, options),
  submitJob: (body, options = {}) => mutation('/jobs', options, body),
  cancelJob: (id, options = {}) => mutation(`/jobs/${segment(id)}/actions/cancel`, options),
  resumeJob: (id, options = {}) => mutation(`/jobs/${segment(id)}/actions/resume`, options),
  retryStage: (id, stage, options = {}) => mutation(`/jobs/${segment(id)}/stages/${segment(stage)}/retry`, options),
  rediscoverSubtitles: (id, options = {}) => mutation(`/jobs/${segment(id)}/subtitles/rediscover`, options),
  selectSubtitle: (id, candidateId, options = {}) => mutation(`/jobs/${segment(id)}/subtitle-candidates/${segment(candidateId)}/select`, options),
  uploadSubtitle: (id, file, options = {}) => {
    const body = new FormData()
    body.append('file', file)
    return mutation(`/jobs/${segment(id)}/subtitles/upload`, { ...options, body })
  },
  publish: (id, platform, options = {}) => mutation(`/jobs/${segment(id)}/publish/${segment(platform)}`, options),
  retryPublish: (id, platform, options = {}) => mutation(`/jobs/${segment(id)}/publish/${segment(platform)}/retry`, options),
  reconcilePublish: async (id, platform, body, options = {}) => mutation(
    `/jobs/${segment(id)}/publish/${segment(platform)}`,
    options,
    reconciliationBody(body),
  ),
  publishVideo: (id, platform, options = {}) => mutation(`/jobs/${segment(id)}/publish/${segment(platform)}`, options),
  refreshStats: (id, options = {}) => mutation(`/jobs/${segment(id)}/stats/refresh`, options),
  getAggregateCosts: (params = {}, options = {}) => request('/costs', { ...options, query: params }),
  getReleases: (id, options = {}) => request(id ? `/releases/${segment(id)}` : '/releases', options),
  getRevenue: (id, options = {}) => request('/revenue', { ...options, query: id ? { identifier: id } : {} }),
  getAlerts: (limit = 50, options = {}) => request('/alerts', { ...options, query: { limit } }),
  getLeaderboard: (options = {}) => request('/leaderboard', options),
  getAnalysis: (id, options = {}) => request(`/analysis/${segment(id)}`, options),
  getPlatformStats: (id, options = {}) => request(`/jobs/${segment(id)}/platform-stats`, options),
  getSegmentInfo: (id, name, options = {}) => request(`/videos/${segment(id)}/segments/${segment(name)}`, options),
  videoUrl: (id) => `${API_BASE}/videos/${segment(id)}`,
  previewUrl: (id) => `${API_BASE}/jobs/${segment(id)}/preview`,
  frameUrl: (id, name, frame) => `${API_BASE}/videos/${segment(id)}/frames/${segment(name)}/${segment(frame)}`,
  fetchVideo: (id, options = {}) => request(`/videos/${segment(id)}`, { ...options, responseType: 'blob' }),
  fetchPreview: (id, options = {}) => request(`/jobs/${segment(id)}/preview`, { ...options, responseType: 'blob' }),
  fetchFrame: (id, name, frame, options = {}) => request(`/videos/${segment(id)}/frames/${segment(name)}/${segment(frame)}`, { ...options, responseType: 'blob' }),
}
