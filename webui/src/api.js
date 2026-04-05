const API = '/api'

async function request(path, options = {}) {
  const res = await fetch(`${API}${path}`, options)
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || 'Request failed')
  }
  return res.json()
}

export const api = {
  // Jobs
  listJobs: (params = {}) => {
    const q = new URLSearchParams(params).toString()
    return request(`/jobs${q ? '?' + q : ''}`)
  },
  getJob: (id) => request(`/jobs/${id}`),
  getJobSteps: (id) => request(`/jobs/${id}/steps`),
  getJobCosts: (id) => request(`/jobs/${id}/costs`),
  submitJob: (body) => request('/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }),

  // Videos & Frames
  videoUrl: (id) => `${API}/videos/${id}`,
  getSegmentInfo: (id, segment) => request(`/videos/${id}/segments/${segment}`),
  frameUrl: (id, segment, frame) => `${API}/videos/${id}/frames/${segment}/${frame}`,

  // Costs
  getAggregateCosts: (params = {}) => {
    const q = new URLSearchParams(params).toString()
    return request(`/costs${q ? '?' + q : ''}`)
  },

  // Leaderboard
  getLeaderboard: () => request('/leaderboard'),

  // Alerts
  getAlerts: (limit = 50) => request(`/alerts?limit=${limit}`),

  // Releases
  getReleases: (imdbId) => {
    if (imdbId) return request(`/releases/${imdbId}`)
    return request('/releases')
  },

  // Revenue (stubbed)
  getRevenue: (imdbId) => {
    const q = imdbId ? `?imdb_id=${imdbId}` : ''
    return request(`/revenue${q}`)
  },

  // Analysis
  getAnalysis: (id) => request(`/analysis/${id}`),
}
