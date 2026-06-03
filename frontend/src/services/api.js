const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000'

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, options)
  if (!response.ok) {
    const detail = await response.text()
    throw new Error(detail || `HTTP ${response.status}`)
  }
  return response.json()
}

export const api = {
  base: API_BASE,
  health: () => request('/api/health'),
  stats: () => request('/api/analysis/stats/overview'),
  listTasks: () => request('/api/tasks'),
  createTask: (payload) =>
    request('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }),
  listSubmissions: (taskId) => request(`/api/submissions${taskId ? `?task_id=${taskId}` : ''}`),
  listJobs: () => request('/api/analysis/jobs'),
  getDetail: (jobId) => request(`/api/analysis/jobs/${jobId}/detail`),
  rerun: (jobId) => request(`/api/analysis/jobs/${jobId}/rerun`, { method: 'POST' }),
  steps: () => request('/api/catalog/steps'),
  rules: () => request('/api/catalog/rules'),
  knowledge: () => request('/api/catalog/knowledge'),
  videoUrl: (submissionId) => `${API_BASE}/api/submissions/${submissionId}/video`,
  uploadSubmission: (formData) =>
    request('/api/submissions', {
      method: 'POST',
      body: formData
    })
}
