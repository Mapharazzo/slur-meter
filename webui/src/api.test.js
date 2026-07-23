import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api, createIdempotencyKey, request } from './api'

function jsonResponse(body, init = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
}

afterEach(() => {
  vi.useRealTimers()
})

describe('request', () => {
  it('sends explicit nonblank authentication without leaking it elsewhere', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await request('/api/jobs', { token: '  operator-secret  ' })
    const [url, options] = fetchMock.mock.calls[0]

    expect(url).toBe('/api/jobs')
    expect(options.headers.get('Authorization')).toBe('Bearer operator-secret')
    expect(url).not.toContain('operator-secret')
  })

  it('keeps health public and ignores blank tokens', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: 'ok' }))
    vi.stubGlobal('fetch', fetchMock)

    await api.health({ token: '   ' })

    expect(fetchMock.mock.calls[0][0]).toBe('/api/health')
    expect(fetchMock.mock.calls[0][1].headers.has('Authorization')).toBe(false)
  })

  it.each(['https://evil.test/api/jobs', '//evil.test/api/jobs'])('rejects unsafe path %s before fetch', async (path) => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(request(path, { token: 'secret' })).rejects.toMatchObject({ code: 'invalid_request' })
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it.each(['/api/../../outside', '/api/%2e%2e/outside'])('rejects normalized API traversal %s before fetch', async (path) => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(request(path, { token: 'secret' })).rejects.toMatchObject({ code: 'invalid_request' })
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('rejects a dot-segment helper identifier before fetch', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(api.getJob('..', { token: 'secret' })).rejects.toMatchObject({ code: 'invalid_request' })
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('encodes strict path segments and omits nullish query values', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [] }))
    vi.stubGlobal('fetch', fetchMock)

    await api.listJobEvents('job /?#', { after: 0, cursor: null, unused: undefined }, { token: 't' })

    expect(fetchMock.mock.calls[0][0]).toBe('/api/jobs/job%20%2F%3F%23/events?after=0')
  })

  it('preserves caller headers and signal while adding JSON content type only for JSON bodies', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: 'job_1' }, { status: 201 }))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()

    await request('/jobs', {
      method: 'POST',
      token: 'token',
      headers: { 'X-Trace': 'trace' },
      signal: controller.signal,
      json: { imdb_id: 'tt0110912' },
    })

    const options = fetchMock.mock.calls[0][1]
    expect(options.headers.get('X-Trace')).toBe('trace')
    expect(options.headers.get('Content-Type')).toBe('application/json')
    expect(options.body).toBe('{"imdb_id":"tt0110912"}')
    expect(options.signal).not.toBe(controller.signal)
    expect(options.signal.aborted).toBe(false)
  })

  it('does not override a browser multipart boundary for FormData', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ candidate: {} }))
    vi.stubGlobal('fetch', fetchMock)
    const form = new FormData()
    form.append('file', new Blob(['1\n00:00:00,000 --> 00:00:01,000\nHi\n']), 'subtitle.srt')

    await request('/jobs/job_1/subtitles/upload', { method: 'POST', body: form })

    const options = fetchMock.mock.calls[0][1]
    expect(options.body).toBe(form)
    expect(options.headers.has('Content-Type')).toBe(false)
  })

  it('parses the structured backend error into safe fields', async () => {
    const payload = {
      error: {
        code: 'job_conflict',
        message: 'The run cannot be resumed.',
        retryable: false,
        details: { actions: ['refresh'] },
        request_id: 'req_123',
      },
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(payload, { status: 409 })))

    const error = await request('/jobs/job_1').catch((value) => value)

    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({
      status: 409,
      code: 'job_conflict',
      message: 'The run cannot be resumed.',
      retryable: false,
      details: { actions: ['refresh'] },
      requestId: 'req_123',
    })
    expect(error).not.toHaveProperty('body')
  })

  it('redacts the caller token if a backend error envelope echoes it', async () => {
    const token = 'operator-secret-value'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({
      error: {
        code: 'unsafe_upstream',
        message: `Upstream echoed ${token}`,
        retryable: false,
        details: { diagnostic: `Bearer ${token}` },
        request_id: `req_${token}`,
      },
    }, { status: 500 })))

    const error = await request('/jobs', { token }).catch((value) => value)

    expect(JSON.stringify(error)).not.toContain(token)
    expect(error.message).toContain('[redacted]')
  })

  it.each([
    ['HTML', new Response('<h1>secret upstream page</h1>', { status: 502, headers: { 'Content-Type': 'text/html' } })],
    ['wrong content type', new Response('{"error":{}}', { status: 500, headers: { 'Content-Type': 'text/plain' } })],
    ['malformed JSON', new Response('{', { status: 500, headers: { 'Content-Type': 'application/json' } })],
    ['malformed envelope', jsonResponse({ detail: 'raw secret body' }, { status: 400 })],
    ['missing envelope fields', jsonResponse({ error: { code: 'bad', message: 'raw secret body', retryable: false } }, { status: 400 })],
    ['array envelope details', jsonResponse({ error: { code: 'bad', message: 'raw secret body', retryable: false, details: ['raw secret body'], request_id: 'req_1' } }, { status: 400 })],
    ['null envelope request ID', jsonResponse({ error: { code: 'bad', message: 'raw secret body', retryable: false, details: {}, request_id: null } }, { status: 400 })],
  ])('returns a generic safe error for %s responses', async (_label, response) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response))

    const error = await request('/jobs').catch((value) => value)

    expect(error).toBeInstanceOf(ApiError)
    expect(['invalid_response', 'http_error']).toContain(error.code)
    expect(error.message).not.toMatch(/secret|upstream|raw/i)
    expect(JSON.stringify(error)).not.toMatch(/secret|upstream|raw/i)
  })

  it('rejects malformed successful JSON and accepts deliberate empty success', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(new Response('{', { headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(null, { status: 204 })))

    await expect(request('/jobs')).rejects.toMatchObject({ code: 'invalid_response' })
    await expect(request('/jobs/job_1/actions/cancel', { method: 'POST' })).resolves.toBeNull()
  })

  it('parses structured JSON errors from authenticated media requests', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({
      error: {
        code: 'artifact_missing',
        message: 'The video is not available.',
        retryable: false,
        details: { actions: ['refresh'] },
        request_id: 'req_media',
      },
    }, { status: 404 })))

    await expect(api.fetchVideo('job_1', { token: 'operator-token' })).rejects.toMatchObject({
      status: 404,
      code: 'artifact_missing',
      message: 'The video is not available.',
      retryable: false,
      requestId: 'req_media',
    })
  })

  it('keeps non-JSON media failures generic and safe', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<h1>raw secret upstream</h1>', {
      status: 502,
      headers: { 'Content-Type': 'text/html' },
    })))

    const error = await api.fetchPreview('job_1', { token: 'operator-token' }).catch((value) => value)
    expect(error).toMatchObject({ status: 502, code: 'invalid_response' })
    expect(JSON.stringify(error)).not.toMatch(/raw|secret|upstream/i)
  })

  it('distinguishes caller abort, timeout, and network failure', async () => {
    vi.useFakeTimers()
    const abortingFetch = vi.fn((_url, { signal }) => new Promise((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })
    }))
    vi.stubGlobal('fetch', abortingFetch)
    const caller = new AbortController()
    const aborted = request('/jobs', { signal: caller.signal, timeoutMs: 1_000 })
    caller.abort()
    await expect(aborted).rejects.toMatchObject({ code: 'aborted', retryable: false })

    const timedOut = expect(request('/jobs', { timeoutMs: 50 })).rejects.toMatchObject({ code: 'timeout', retryable: true })
    await vi.advanceTimersByTimeAsync(50)
    await timedOut

    vi.useRealTimers()
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('token secret socket body')))
    const networkError = await request('/jobs').catch((value) => value)
    expect(networkError).toMatchObject({ code: 'network_error', retryable: true })
    expect(networkError.message).not.toContain('secret')
  })

  it('enforces its timeout even if the fetch implementation ignores abort', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))

    const timedOut = expect(request('/jobs', { timeoutMs: 25 })).rejects.toMatchObject({ code: 'timeout' })
    await vi.advanceTimersByTimeAsync(25)
    await timedOut
  }, 1_000)

  it('enforces its timeout while successful JSON parsing is stalled', async () => {
    vi.useFakeTimers()
    const response = jsonResponse({ never: 'parsed' })
    vi.spyOn(response, 'json').mockImplementation(() => new Promise(() => {}))
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response))

    const timedOut = expect(request('/jobs', { timeoutMs: 25 })).rejects.toMatchObject({ code: 'timeout' })
    await vi.advanceTimersByTimeAsync(25)
    await timedOut
  }, 1_000)

  it('cleans caller abort listeners after success', async () => {
    vi.useFakeTimers()
    const caller = new AbortController()
    const add = vi.spyOn(caller.signal, 'addEventListener')
    const remove = vi.spyOn(caller.signal, 'removeEventListener')
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ ok: true })))

    await request('/health', { signal: caller.signal })

    expect(add).toHaveBeenCalledWith('abort', expect.any(Function), expect.anything())
    expect(remove).toHaveBeenCalledWith('abort', expect.any(Function))
    expect(vi.getTimerCount()).toBe(0)
  })

  it.each([
    [{ token: 'secret\r\ninjected' }, 'token'],
    [{ idempotencyKey: 'key\u0000bad', method: 'POST' }, 'idempotency key'],
  ])('turns an unsafe %s header into a safe validation error', async (options, label) => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(request('/jobs', options)).rejects.toMatchObject({
      code: 'invalid_request',
      message: expect.stringMatching(new RegExp(label, 'i')),
    })
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('uses one reusable explicit idempotency key on operator mutations', async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse({ changed: true })))
    vi.stubGlobal('fetch', fetchMock)
    const key = createIdempotencyKey()

    await api.cancelJob('job_1', { token: 't', idempotencyKey: key })
    await api.cancelJob('job_1', { token: 't', idempotencyKey: key })

    expect(key).toMatch(/^ui_[a-zA-Z0-9_-]+$/)
    expect(fetchMock.mock.calls.map(([, options]) => options.headers.get('Idempotency-Key'))).toEqual([key, key])
  })

  it('exposes encoded credential-free media URLs', () => {
    expect(api.videoUrl('job /1')).toBe('/api/videos/job%20%2F1')
    expect(api.previewUrl('job /1')).toBe('/api/jobs/job%20%2F1/preview')
    expect(api.frameUrl('job /1', 'graph/alt', 4)).toBe('/api/videos/job%20%2F1/frames/graph%2Falt/4')
    expect(api.videoUrl('job_1', { token: 'never-embed-me' })).not.toContain('never-embed-me')
  })
})

describe('api routes', () => {
  it('maps operational read and mutation routes exactly', async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse({ ok: true })))
    vi.stubGlobal('fetch', fetchMock)
    const mutation = { token: 't', idempotencyKey: 'same-key' }

    await api.operationsSummary({ token: 't' })
    await api.getJob('job_1', { token: 't' })
    await api.retryStage('job_1', 'subtitle selection', mutation)
    await api.selectSubtitle('job_1', 'candidate/1', mutation)
    await api.publish('job_1', 'youtube', mutation)
    await api.retryPublish('job_1', 'youtube', mutation)
    await api.reconcilePublish('job_1', 'youtube', { reconciliation: 'uploaded', remote_id: 'video_1' }, mutation)

    expect(fetchMock.mock.calls.map(([url, options]) => [url, options.method])).toEqual([
      ['/api/operations/summary', 'GET'],
      ['/api/jobs/job_1', 'GET'],
      ['/api/jobs/job_1/stages/subtitle%20selection/retry', 'POST'],
      ['/api/jobs/job_1/subtitle-candidates/candidate%2F1/select', 'POST'],
      ['/api/jobs/job_1/publish/youtube', 'POST'],
      ['/api/jobs/job_1/publish/youtube/retry', 'POST'],
      ['/api/jobs/job_1/publish/youtube', 'POST'],
    ])
  })

  it('keeps publish credentials in headers and reconciliation in an explicit body', async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse({ changed: true })))
    vi.stubGlobal('fetch', fetchMock)
    const options = { token: 'operator-token', idempotencyKey: 'publish-key' }

    await api.publish('job_1', 'youtube', options)
    await api.retryPublish('job_1', 'youtube', options)
    await api.reconcilePublish('job_1', 'youtube', { reconciliation: 'not_uploaded' }, options)

    for (const [, call] of fetchMock.mock.calls) {
      expect(call.headers.get('Authorization')).toBe('Bearer operator-token')
      expect(call.headers.get('Idempotency-Key')).toBe('publish-key')
    }
    expect(fetchMock.mock.calls[0][1].body).toBeUndefined()
    expect(fetchMock.mock.calls[1][1].body).toBeUndefined()
    expect(fetchMock.mock.calls[2][1].body).toBe('{"reconciliation":"not_uploaded"}')
  })

  it('rejects malformed reconciliation before fetch', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(api.reconcilePublish('job_1', 'youtube', {
      reconciliation: 'uploaded',
      remote_id: 'unsafe/remote',
    }, { token: 'operator-token' })).rejects.toMatchObject({ code: 'invalid_request' })
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
