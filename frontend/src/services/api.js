/**
 * 后端接口封装。
 *
 * **同源是硬约束，不是风格选择。** 会话靠 httpOnly cookie 维持，而 cookie 的
 * SameSite=Lax 在跨源下会被浏览器当成第三方 cookie，XHR 一律不带——
 * 症状是"登录接口返回成功，但下一个请求还是 401"。
 * 所以这里用相对路径（生产环境由 FastAPI 直接托管前端构建产物），
 * 开发环境由 vite 的 proxy 转发到后端。绝对 URL 一律不要出现。
 */

// 相对路径。开发时走 vite proxy，生产时同源直连。
const API_BASE = ''

/** 401 时由 App.vue 注册的回调：踢回登录页。 */
let onUnauthorized = null

export function setUnauthorizedHandler(handler) {
  onUnauthorized = handler
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/**
 * 把后端的错误响应变成人能读的一句话。
 *
 * FastAPI 的错误体是 `{"detail": "..."}`，直接 `response.text()` 会把这串
 * JSON 原样显示给学生。所以要先解析，解析不出来才退回原文。
 */
async function toErrorMessage(response) {
  const raw = await response.text()
  if (!raw) return `请求失败（HTTP ${response.status}）`
  try {
    const parsed = JSON.parse(raw)
    const detail = parsed?.detail
    if (typeof detail === 'string') return detail
    // 422 校验错误的 detail 是一个数组，取第一条的 msg。
    if (Array.isArray(detail) && detail.length) return detail[0]?.msg || raw
  } catch {
    /* 不是 JSON，按原文返回 */
  }
  return raw
}

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    // 同源下这是默认值，但写出来是为了防止将来有人把 API_BASE 改成绝对地址——
    // 那时这行至少还提醒了他 cookie 必须带上。
    credentials: 'same-origin',
    ...options
  })

  if (response.status === 401) {
    // 会话过期/被停用：全局踢回登录页，而不是让每个页面各写一遍。
    onUnauthorized?.()
  }
  if (!response.ok) {
    throw new ApiError(await toErrorMessage(response), response.status)
  }
  if (response.status === 204) return null
  return response.json()
}

function json(payload) {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  }
}

export const api = {
  base: API_BASE,

  // --- 鉴权 ---------------------------------------------------------------
  login: (username, password) => request('/api/auth/login', json({ username, password })),
  logout: () => request('/api/auth/logout', { method: 'POST' }),
  me: () => request('/api/auth/me'),
  changePassword: (oldPassword, newPassword) =>
    request('/api/auth/change-password', json({ old_password: oldPassword, new_password: newPassword })),

  // --- 基础数据 -----------------------------------------------------------
  health: () => request('/api/health'),
  stats: () => request('/api/analysis/stats/overview'),
  steps: () => request('/api/catalog/steps'),
  rules: () => request('/api/catalog/rules'),
  knowledge: () => request('/api/catalog/knowledge'),

  // --- 任务与提交 ---------------------------------------------------------
  listTasks: () => request('/api/tasks'),
  createTask: (payload) => request('/api/tasks', json(payload)),
  listSubmissions: (taskId) =>
    request(`/api/submissions${taskId ? `?task_id=${taskId}` : ''}`),
  listJobs: () => request('/api/analysis/jobs'),
  getDetail: (jobId) => request(`/api/analysis/jobs/${jobId}/detail`),
  rerun: (jobId) => request(`/api/analysis/jobs/${jobId}/rerun`, { method: 'POST' }),
  queuePosition: (jobId) => request(`/api/analysis/jobs/${jobId}/queue`),

  // 证据帧同样走同源相对路径；后端带 cookie 鉴权，学生看不到别人的帧。
  videoUrl: (submissionId) => `/api/submissions/${submissionId}/video`,
  frameUrl: (submissionId, index) => `/api/submissions/${submissionId}/frames/${index}`,

  // --- 名册 ---------------------------------------------------------------
  importRoster: (payload) => request('/api/roster/import', json(payload)),
  listRoster: (taskId) => request(`/api/roster/${taskId}`),

  // --- 教师复核 -----------------------------------------------------------
  reviewQueue: () => request('/api/reviews/queue'),
  reviewViolation: (jobId, violationId, action, comment) =>
    request(`/api/reviews/jobs/${jobId}/violations/${violationId}`, json({ action, comment })),
  reviewStep: (jobId, stepId, verdict, comment) =>
    request(`/api/reviews/jobs/${jobId}/steps/${stepId}`, json({ verdict, comment })),
  finalize: (jobId, comment) => request(`/api/reviews/jobs/${jobId}/finalize`, json({ comment })),
  reopen: (jobId, comment) => request(`/api/reviews/jobs/${jobId}/reopen`, json({ comment })),
  appeal: (jobId, message) => request(`/api/reviews/jobs/${jobId}/appeal`, json({ message })),
  listAppeals: () => request('/api/reviews/appeals'),
  exportUrl: (taskId) => `/api/reviews/export.csv?task_id=${taskId}`,

  // --- 用户管理 -----------------------------------------------------------
  listUsers: () => request('/api/users'),
  createUser: (payload) => request('/api/users', json(payload)),
  resetPassword: (userId, newPassword) =>
    request(`/api/users/${userId}/reset-password`, json({ new_password: newPassword })),
  setActive: (userId, isActive) =>
    request(`/api/users/${userId}/active`, json({ is_active: isActive }))
}

/**
 * 上传视频。
 *
 * **必须用 XMLHttpRequest，不能用 fetch。** fetch 没有上传进度事件，
 * 而"100 个学生在实训室同时上传"是真实的峰值场景——没有进度条的话，
 * 学生看到的是一个几分钟不动的空白，会反复点提交或以为系统坏了。
 *
 * 走同一个 XHR 也顺便解决了另一个问题：超时和中断都能被明确报告出来，
 * 而不是变成一个语焉不详的 "Failed to fetch"。
 */
export function uploadSubmission({ taskId, file, onProgress, timeoutMs = 10 * 60 * 1000 }) {
  return new Promise((resolve, reject) => {
    const form = new FormData()
    form.append('task_id', String(taskId))
    form.append('file', file)

    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${API_BASE}/api/submissions`)
    xhr.withCredentials = true // 同源下也是默认，但显式声明避免将来改 base 时踩坑
    xhr.timeout = timeoutMs

    xhr.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(Math.round((event.loaded / event.total) * 100))
      }
    })

    xhr.addEventListener('load', () => {
      if (xhr.status === 401) onUnauthorized?.()
      let payload = null
      try {
        payload = JSON.parse(xhr.responseText)
      } catch {
        /* 非 JSON 响应，下面按状态码处理 */
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(payload)
        return
      }
      const detail = payload?.detail
      reject(
        new ApiError(
          typeof detail === 'string' ? detail : `上传失败（HTTP ${xhr.status}）`,
          xhr.status
        )
      )
    })

    xhr.addEventListener('error', () =>
      reject(new ApiError('网络中断，上传失败。请检查是否还连着实训室的网络。', 0))
    )
    xhr.addEventListener('timeout', () =>
      reject(new ApiError('上传超时。视频较大时请换用网线，或分几次上传。', 0))
    )
    xhr.addEventListener('abort', () => reject(new ApiError('上传已取消。', 0)))

    xhr.send(form)
  })
}
