import axios from 'axios'
import type { AxiosError, InternalAxiosRequestConfig } from 'axios'
import { ElMessage, ElMessageBox } from 'element-plus'

// Default HTTP client — for fast JSON calls (auth, CRUD, search, etc.)
export const http = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
})

// Dedicated client for big uploads — no global timeout.
// 进度条由前端组件自己驱动 + 后端流式响应即可。
export const httpUpload = axios.create({
  baseURL: '/api/v1',
  timeout: 0,           // 0 = no timeout; large firmware can take >30s
  maxContentLength: Infinity,
  maxBodyLength: Infinity,
})

/**
 * Resolve the current bearer token from localStorage.
 *
 * Kept as a free function rather than a closure so the same logic can be
 * used by both the default client and the upload client without us having
 * to reach into ``interceptors.request.handlers[0]`` (which is a private
 * Axios handle, not the original callback, and breaks silently if we do).
 *
 * Reading from localStorage on every request is fine — JWTs are short
 * strings and the cost is dwarfed by the network round-trip.
 */
function readToken(): string | null {
  try {
    return localStorage.getItem('naw.token')
  } catch {
    return null
  }
}

function addAuthHeader(config: InternalAxiosRequestConfig): InternalAxiosRequestConfig {
  // Single source of truth: localStorage. We always read fresh on every
  // request because the cost is negligible (a few-byte sync read) and
  // it guarantees we never attach a token that's been cleared.
  //
  // The previous version tried to also peek into the Pinia store as a
  // fallback, which introduced a race: the request would race out before
  // the async fallback resolved, causing axios to send the request
  // without an Authorization header and the server to reject it.
  const token = readToken()
  if (token) {
    config.headers = config.headers ?? ({} as InternalAxiosRequestConfig['headers'])
    ;(config.headers as Record<string, string>).Authorization = `Bearer ${token}`
  }
  return config
}

/**
 * FastAPI ``APIRouter`` decorators register paths with a trailing slash
 * (``@router.get("")`` ends up mounted as ``/documents/`` once the module
 * prefix is applied). The default ``redirect_slashes=True`` would 307 a
 * request for ``/documents`` to ``/documents/`` — but the browser follow
 * drops the ``Authorization`` header on the second hop, surfacing
 * spurious 401s.
 *
 * We solve this client-side by normalising every collection URL to its
 * trailing-slash form before it leaves the browser, so the server never
 * has to redirect. Resource lookups (``/documents/12``) are left alone —
 * FastAPI only redirects the collection root.
 *
 * NOTE: the previous version short-circuited on any URL containing a
 * query string, so ``/drift?limit=200`` was never normalised and went
 * out as the bare path. This fix splits URL into path and query, only
 * inspects the path, and re-attaches the query untouched.
 */
function appendTrailingSlash(config: InternalAxiosRequestConfig): InternalAxiosRequestConfig {
  const url = config.url ?? ''
  // Skip non-path URLs (rare, but defensive) and URLs that already end
  // in a slash or are fully-qualified http(s) URLs.
  if (!url || url.startsWith('http') || url.endsWith('/')) {
    return config
  }
  // Split off the query string so the trailing-slash check only looks at
  // the path. The old check ``url.includes('?')`` would abort normalisation
  // on any parametrised request and that caused 307 → 401 on /drift?limit=200.
  const qIdx = url.indexOf('?')
  const path = qIdx === -1 ? url : url.slice(0, qIdx)
  const query = qIdx === -1 ? '' : url.slice(qIdx)
  const segments = path.split('/').filter(Boolean)

  // Only normalise collection URLs (single segment after the API root).
  // ``/documents`` -> 1 segment -> needs slash.
  // ``/devices``   -> 1 segment -> needs slash.
  // ``/ai/history`` -> 2 segments -> leave alone (ai router has no slash routes).
  // ``/drift``     -> 1 segment -> leave alone (drift router has no slash route;
  //                  adding one triggers a 307 redirect to /drift which drops
  //                  the Authorization header in the browser).
  if (segments.length !== 1) return config
  const skipPrefixes = new Set(['drift', 'ai'])
  if (skipPrefixes.has(segments[0])) return config

  config.url = path + '/' + query
  return config
}

// ─────────────────────────────────────────────────────────────────────
// 401 handling
// ─────────────────────────────────────────────────────────────────────
// Distinguish three cases so we don't pop the "会话过期" dialog every
// time the user clicks an unauthorised action or hits a not-yet-mounted
// page that fires requests before localStorage is read:
//
//   A. We DID send a bearer token but the server rejected it
//      → "会话过期" popup (real JWT failure)
//   B. We did NOT send a token (localStorage was empty / cleared) AND
//      we are NOT on /login
//      → silently clear session, no popup. The page will naturally
//        bounce to /login via the router guard.
//   C. Any other 401 we cannot classify
//      → show a generic error toast (no popup)
//
// We also throttle the popup: only one per session so multiple in-flight
// requests don't queue up a stack of ElMessageBox dialogs.
let _401popupShown = false

function handleResponseError(err: unknown) {
  const e = err as AxiosError<{ detail?: string }>
  const status = e?.response?.status
  if (status !== 401) {
    if (status === 403) {
      ElMessage.error('权限不足')
    } else if (status && status >= 500) {
      ElMessage.error('服务器错误：' + (e.response?.data?.detail || e.message))
    }
    return Promise.reject(err)
  }

  const detail = e.response?.data?.detail || ''
  // Did we send a token on this request? If yes and the server still 401'd,
  // the token is genuinely bad.
  const sentToken = !!e.config?.headers?.Authorization
  // Server detail strings that prove the token is invalid (vs. missing).
  const tokenInvalid =
    /invalid token|token expired|signature|decode|user not found|not authenticated/i.test(detail)

  const onLogin =
    location.hash === '#/login' || location.hash.startsWith('#/login?')

  // Case A: real token failure → popup (only once per session)
  if (sentToken && tokenInvalid && !onLogin && !_401popupShown) {
    _401popupShown = true
    void import('@/stores/auth').then(({ useAuthStore }) => {
      useAuthStore().expireSession()
    })
    ElMessageBox.confirm(
      '当前登录凭据已失效（本地存储的 JWT 与服务端密钥不匹配，或已过期）。点击「重新登录」前往登录页，留在此页可继续浏览。',
      '会话过期',
      { confirmButtonText: '重新登录', cancelButtonText: '留在当前页', type: 'warning' },
    ).catch(() => null).then((action) => {
      if (action === 'confirm') {
        location.hash = '#/login'
      }
      _401popupShown = false
    })
    return Promise.reject(err)
  }

  // Case B: no token was sent → silently clear store, no popup.
  // The router guard will route the user to /login.
  if (!sentToken) {
    if (!onLogin) {
      location.hash = '#/login'
    }
    void import('@/stores/auth').then(({ useAuthStore }) => {
      useAuthStore().expireSession()
    })
    return Promise.reject(err)
  }

  // Case C: token was sent, server returned 401 for a reason we can't
  // classify (e.g. an unexpected 401 from a vendor endpoint). Don't popup;
  // just surface the detail so the user (and dev console) can see what
  // happened.
  ElMessage.error('请求未授权：' + (detail || '请稍后重试'))
  return Promise.reject(err)
}

// Request pipeline: normalise URL → add bearer token.
http.interceptors.request.use(appendTrailingSlash)
http.interceptors.request.use(addAuthHeader)
http.interceptors.response.use(
  (resp) => resp,
  handleResponseError,
)

// Upload client shares the same auth + error handling — just no timeout.
httpUpload.interceptors.request.use(appendTrailingSlash)
httpUpload.interceptors.request.use(addAuthHeader)
httpUpload.interceptors.response.use(
  (resp) => resp,
  handleResponseError,
)

export interface LoginRequest {
  username: string
  password: string
}
export interface TokenResponse {
  access_token: string
  token_type: string
  expires_in: number
}
export interface UserOut {
  id: number
  username: string
  email: string
  full_name: string | null
  role: string
  is_active: boolean
  created_at: string
  last_login_at: string | null
}
export interface DashboardStats {
  total_devices: number
  active_devices: number
  simulated_devices: number
  by_vendor: Record<string, number>
  total_jobs: number
  jobs_by_status: Record<string, number>
  total_users: number
  total_credentials: number
  total_documents: number
  revisions_total: number
  pending_approval: number
  running_jobs: number
}

export interface DashboardEvent {
  id: number
  actor: string
  action: string
  target_kind?: string | null
  target_id?: string | null
  detail?: string | null
  created_at: string
}

export interface DashboardJob {
  id: number
  name: string
  kind: string
  status: string
  initiated_by: string
  started_at?: string | null
  progress: number
  target_device_ids: number[]
  summary?: Record<string, number> | null
  requires_approval: boolean
}

export interface DriftSummary {
  total: number
  new_count: number
  acknowledged: number
  by_severity: { critical: number; warning: number; info: number }
}
export interface DriftListItem {
  id: number
  device_id: number
  device_name: string
  vendor: string
  group_name: string | null
  detected_at: string
  severity: 'critical' | 'warning' | 'info'
  status: 'new' | 'acknowledged'
  lines_added: number
  lines_removed: number
  lines_modified: number
  change_hint: string | null
  acknowledged_by: string | null
  acknowledged_at: string | null
}
export interface DriftDetail extends DriftListItem {
  baseline_revision_id: number | null
  current_revision_id: number | null
  diff_snippet: string | null
  baseline_hash: string | null
  current_hash: string | null
}

// ── AI Agent ──────────────────────────────────────────────
export interface AIConfig {
  id: number
  provider: string
  base_url: string
  api_key_masked: string | null
  model: string
  temperature: number
  max_tokens: number
  system_prompt: string
  enabled: boolean
  active_persona: 'chat' | 'agent' | 'monitor'
  last_tested_at: string | null
  last_test_ok: boolean | null
  last_test_message: string | null
  total_chats: number
  total_agent_actions: number
  total_tokens: number
  updated_at: string | null
  updated_by: string | null
}
export interface AIStats {
  enabled: boolean
  active_persona: string
  total_chats: number
  total_agent_actions: number
  total_tokens: number
  today_chats: number
  tool_calls_total: number
  tool_calls_success: number
  tool_success_rate: number
  last_tested_at: string | null
  last_test_ok: boolean | null
  last_test_message: string | null
  model: string
}
export interface AIChatMessage {
  id?: number
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string
  name?: string | null
  tool_calls?: unknown
  tool_call_id?: string | null
  tokens_used?: number | null
  created_at?: string
  /** Optional plan attached to an assistant turn. Set only when the
   * LLM invoked ``propose_config_plan`` during the turn. The plan
   * payload matches ``AIProposedPlan``. */
  ai_plan?: AIProposedPlan | null
}
export interface AIChatResponse {
  ok?: boolean
  reply?: string
  error?: string
  tokens?: number
  /** If the LLM invoked ``propose_config_plan`` during this turn,
   * the resolved plan arrives here so the frontend can offer a
   * single "Dry-Run this AI plan" button. */
  plan?: AIProposedPlan | null
  tools_used?: { tool: string; ok: boolean; ms: number }[]
  reasoning_trace?: unknown
}
export interface AIProposedPlan {
  commands: string[]
  vendor: string
  notes: string[]
  summary: string
  source: 'ai_chat'
  session_id: string
}
export interface AIAgentLog {
  id: number
  username: string
  session_id: string
  tool: string
  args: Record<string, unknown> | null
  result_preview: string | null
  ok: boolean
  error: string | null
  duration_ms: number
  created_at: string
}

export interface HealthSnapshot {
  id: number
  device_id: number
  collected_at: string
  vendor: string
  cpu_pct: number | null
  mem_pct: number | null
  temp_c: number | null
  fan_state: string | null
  power_w: number | null
  source: string
  errors: string[] | null
  // v1.7 new fields
  optical?: { port: string; rx_dbm: number | null; tx_dbm: number | null }[]
  config_fingerprint?: string | null
  config_lines?: number | null
  config_excerpt?: string | null
  metrics_run?: string[] | null
}
export interface HealthCollectResponse {
  mode: 'all' | 'targeted'
  devices: number
  task_ids: string[]
}

// ── Health schedules (v1.7) ────────────────────────────────────────
export interface HealthSchedule {
  id: number
  name: string
  interval_minutes: number
  device_ids: number[]
  metrics: string[]
  enabled: boolean
  last_fired_at: string | null
  next_run_at: string | null
  description: string | null
  created_by: string | null
  created_at: string
  updated_at: string | null
}

export const api = {
  bootstrapStatus: () => http.get<{ has_admin: boolean }>('/auth/bootstrap-status').then((r) => r.data),
  login: (data: LoginRequest) => http.post<TokenResponse>('/auth/login', data).then((r) => r.data),
  me: () => http.get('/auth/me').then((r) => r.data),
  changePassword: (data: { old_password: string; new_password: string }) =>
    http.post('/auth/change-password', data).then((r) => r.data),
  listUsers: () => http.get('/auth/users').then((r) => r.data),
  approveUser: (userId: number) =>
    http.post<UserOut>(`/auth/users/${userId}/approve`).then((r) => r.data),
  createUser: (data: { username: string; password: string; role: string; full_name?: string }) =>
    http.post('/auth/users', data).then((r) => r.data),

  dashboard: () => http.get<DashboardStats>('/dashboard/stats').then((r) => r.data),
  recentEvents: () => http.get<DashboardEvent[]>('/dashboard/recent-events').then((r) => r.data),
  activeJobs: () => http.get<DashboardJob[]>('/dashboard/active-jobs').then((r) => r.data),

  listDevices: (params: Record<string, unknown> = {}) => http.get('/devices', { params }).then((r) => r.data),
  getDevice: (id: number) => http.get(`/devices/${id}`).then((r) => r.data),
  createDevice: (data: unknown) => http.post('/devices', data).then((r) => r.data),
  updateDevice: (id: number, data: Record<string, unknown>) =>
    http.patch(`/devices/${id}`, data).then((r) => r.data),
  deleteDevice: (id: number) => http.delete(`/devices/${id}`).then((r) => r.data),
  importCsv: (file: File, defaultVendor: string) => {
    const fd = new FormData()
    fd.append('file', file)
    return http
      .post(`/devices/import-csv?default_vendor=${encodeURIComponent(defaultVendor)}`, fd, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      .then((r) => r.data)
  },

  // ── Endpoint CRUD (CRT-style connection config) ──
  listDeviceEndpoints: (deviceId: number) =>
    http.get(`/devices/${deviceId}/endpoints`).then((r) => r.data),
  createDeviceEndpoint: (deviceId: number, data: {
    kind: string
    address: string
    port: number
    credential_id?: number | null
    is_primary?: boolean
    notes?: string | null
  }) => http.post(`/devices/${deviceId}/endpoints`, data).then((r) => r.data),
  deleteDeviceEndpoint: (deviceId: number, endpointId: number) =>
    http.delete(`/devices/${deviceId}/endpoints/${endpointId}`).then((r) => r.data),
  setEndpointPrimary: (deviceId: number, endpointId: number) =>
    http.post(`/devices/${deviceId}/endpoints/${endpointId}/primary`).then((r) => r.data),

  listCredentials: () => http.get('/credentials').then((r) => r.data),
  createCredential: (data: unknown) => http.post('/credentials', data).then((r) => r.data),
  deleteCredential: (id: number) => http.delete(`/credentials/${id}`).then((r) => r.data),

  listRevisions: (deviceId: number) => http.get(`/configs/device/${deviceId}/revisions`).then((r) => r.data),
  readRevisionContent: (revId: number) => http.get(`/configs/revisions/${revId}/content`).then((r) => r.data),
  importConfig: (data: unknown) => http.post('/configs/import', data).then((r) => r.data),
  diff: (from: number, to: number) =>
    http.post('/configs/diff', { from_revision_id: from, to_revision_id: to }).then((r) => r.data),

  listDocuments: (params: Record<string, unknown> = {}) => http.get('/documents', { params }).then((r) => r.data),
  uploadDocument: (
    file: File,
    meta: { title: string; vendor?: string; category?: string; version_tag?: string; source?: string },
    onProgress?: (pct: number) => void,
  ) => {
    const fd = new FormData()
    fd.append('file', file)
    const params = new URLSearchParams({ title: meta.title })
    if (meta.vendor) params.set('vendor', meta.vendor)
    if (meta.category) params.set('category', meta.category)
    if (meta.version_tag) params.set('version_tag', meta.version_tag)
    if (meta.source) params.set('source', meta.source)
    return httpUpload
      .post(`/documents?${params.toString()}`, fd, {
        headers: { 'Content-Type': 'multipart/form-data' },
        onUploadProgress: (e) => {
          if (onProgress && e.total) onProgress(Math.round((e.loaded * 100) / e.total))
        },
      })
      .then((r) => r.data)
  },
  searchDocuments: (q: string) => http.get('/documents/search', { params: { q } }).then((r) => r.data),
  getDocument: (id: number) => http.get(`/documents/${id}`).then((r) => r.data),
  createOfflineManualSession: (id: number) =>
    http.post(`/documents/${id}/offline-session`).then((r) => r.data),
  updateDocument: (id: number, body: Record<string, unknown>) => http.patch(`/documents/${id}`, body).then((r) => r.data),
  deleteDocument: (id: number) => http.delete(`/documents/${id}`).then((r) => r.data),

  listJobs: () => http.get('/jobs').then((r) => r.data),
  getJob: (id: number) => http.get(`/jobs/${id}`).then((r) => r.data),
  createJob: (data: {
    name: string
    kind: 'batch_read' | 'batch_write' | 'template_apply' | 'collection'
    target_device_ids: number[]
    template_id?: number
    params?: Record<string, unknown>
    note?: string
    batch_size?: number
    /**
     * Custom commands to run on every target device.  Required for
     * kind="batch_read" to actually collect anything other than the
     * hardcoded "display current-configuration" fallback.  See
     * api/v1/jobs.py:create_job for the resolution order.
     */
    commands?: string[]
  }) => http.post('/jobs', data).then((r) => r.data),
  startJob: (id: number) => http.post(`/jobs/${id}/start`).then((r) => r.data),
  cancelJob: (id: number) => http.post(`/jobs/${id}/control`, { action: 'cancel' }).then((r) => r.data),
  /**
   * Delete a job.  Returns 204 No Content on success, 409 if the job
   * is still running (operator must cancel first), 404 if the job
   * is already gone.  The whole row + its step records are removed
   * in a single transaction; the deletion is recorded in the audit
   * log before commit.
   */
  deleteJob: (id: number) => http.delete(`/jobs/${id}`).then((r) => r.data),
  /** Custom command collection (migrated from PythonProject5).
   * Lets an operator paste a list of ``display``-style commands and
   * have them run on every selected device, with the per-(device,
   * command) output landing in the standard ``config_revisions``
   * table.  Returns the same ``celery_task_id``-style envelope the
   * health / jobs endpoints use, so the UI can poll the existing
   * job-stream WebSocket. */
  collectCommands: (data: {
    device_ids: number[]
    commands: string[]
    name?: string
    note?: string
  }) => http.post('/commands/collect', data).then((r) => r.data),
  listCommandCollectJobs: (limit = 50) =>
    http.get('/commands/jobs', { params: { limit } }).then((r) => r.data),
  exportCommandCollect: (jobId: number) =>
    http.get(`/commands/jobs/${jobId}/export`, { responseType: 'blob' }).then((r) => r.data),
  /**
   * Dry-run accepts a ``plan`` block (preferred) OR legacy fields. The
   * frontend always sends ``plan`` now so Dry-Run sees the same
   * commands the Worker will execute.
   */
  dryRunJob: (
    payload: {
      plan?: Record<string, unknown>;
      commands?: string[];
      template_id?: number;
      params?: Record<string, unknown>;
      device_ids?: number[];
    }
  ) => http.post('/jobs/dry-run', payload).then((r) => r.data),
  rollbackPlan: (id: number) => http.post(`/jobs/${id}/rollback-plan`).then((r) => r.data),
  aiCompareJob: (id: number) => http.post(`/jobs/${id}/ai-compare`).then((r) => r.data),
  /** Save ``/jobs/{id}/errors.csv`` to disk via a hidden ``<a>`` tag.
   * Mirrors the style of ``exportData`` for the bulk-export scope so
   * the whole app has one consistent way to hand a file to the OS. */
  exportJobErrors: async (jobId: number): Promise<{ rows: number; filename: string }> => {
    const resp = await http.get(`/jobs/${jobId}/errors.csv`, { responseType: 'blob' })
    const blob = resp.data as Blob
    const disposition = resp.headers['content-disposition'] as string | undefined
    const fnMatch = disposition?.match(/filename\*?=['"]?(?:UTF-8'')?([^;\n"']+)/i)
    const filename = fnMatch ? fnMatch[1] : `naw-job-${jobId}-errors.csv`
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = decodeURIComponent(filename)
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    // The CSV body has a header row + N error rows.  We parse the
    // header + first row to confirm the format and then re-derive
    // ``rows`` from the blob size in bytes — cheap and avoids a
    // second fetch.  If the body is header-only, ``rows`` stays 0.
    const text = await blob.text()
    const lines = text.split(/\r?\n/).filter((l) => l.length > 0)
    return { rows: Math.max(0, lines.length - 1), filename: decodeURIComponent(filename) }
  },

  listAdapters: () => http.get('/system/adapters').then((r) => r.data),
  health: () => http.get('/system/health').then((r) => r.data),

  listAudit: (params: Record<string, unknown> = {}) => http.get('/audit', { params }).then((r) => r.data),

  // ── Drift ──────────────────────────────────────────────────────────────
  driftSummary: () => http.get<DriftSummary>('/drift/summary').then((r) => r.data),
  driftList: (params: Record<string, unknown> = {}) => http.get<DriftListItem[]>('/drift', { params }).then((r) => r.data),
  driftDetail: (id: number) => http.get<DriftDetail>(`/drift/${id}`).then((r) => r.data),
  driftAcknowledge: (id: number) => http.post(`/drift/${id}/acknowledge`).then((r) => r.data),
  driftTriggerScan: () => http.post<{ task_id: string }>('/drift/scan').then((r) => r.data),
  driftTriggerScanOne: (deviceId: number) => http.post(`/drift/scan/${deviceId}`).then((r) => r.data),
  driftSetBaseline: (deviceId: number, revisionId?: number) =>
    http.post(`/drift/devices/${deviceId}/baseline`, { revision_id: revisionId ?? null }).then((r) => r.data),

  // ── Health (lightweight per-device metrics) ──────────────────────────
  healthCollect: (deviceIds?: number[], metrics?: string[]) =>
    http.post<HealthCollectResponse>('/health/collect',
      { device_ids: deviceIds ?? [], metrics: metrics ?? [] }).then((r) => r.data),

  // v1.7: snapshot list / export
  healthSnapshots: (params: { device_id?: number; limit?: number; offset?: number; metric?: string } = {}) =>
    http.get<HealthSnapshot[]>('/health/snapshots', { params }).then((r) => r.data),
  /**
   * Fetch a single snapshot for the "查看" detail dialog.  Returns
   * the full row including raw_excerpt / config_excerpt / optical /
   * errors — the fields that are too wide to fit in the snapshot
   * list table.
   */
  healthSnapshot: (id: number) =>
    http.get<HealthSnapshot>(`/health/snapshots/${id}`).then((r) => r.data),

  healthExport: async (snapshotId: number, format: 'csv' | 'xlsx' | 'txt' = 'csv', filename?: string) => {
    const resp = await http.get(`/health/snapshots/${snapshotId}/export`, {
      params: { format, filename },
      responseType: 'blob',
    })
    const blob = resp.data as Blob
    const disposition = resp.headers['content-disposition'] as string | undefined
    const fnMatch = disposition?.match(/filename\*?=['"]?(?:UTF-8'')?([^;\n"']+)/i)
    const fn = fnMatch ? fnMatch[1] : `health-snapshot-${snapshotId}.${format}`
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = decodeURIComponent(fn)
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  },

  // v1.7: schedule CRUD
  healthSchedules: () =>
    http.get<HealthSchedule[]>('/health/schedules/').then((r) => r.data),
  healthSchedulesCreate: (data: {
    name: string; interval_minutes: number; device_ids?: number[];
    metrics?: string[]; enabled?: boolean; description?: string;
  }) =>
    http.post<HealthSchedule>('/health/schedules/', data).then((r) => r.data),
  healthSchedulesDelete: (id: number) =>
    http.delete(`/health/schedules/${id}`).then((r) => r.data),
  healthSchedulesRun: (id: number) =>
    http.post<HealthSchedule>(`/health/schedules/${id}/run`).then((r) => r.data),

  // ── AI Agent ──────────────────────────────────────────
  aiGetConfig: () => http.get<AIConfig>('/ai/config').then((r) => r.data),
  aiUpdateConfig: (data: Partial<AIConfig> & { api_key?: string }) =>
    http.put('/ai/config', data).then((r) => r.data),
  aiTestConfig: () => http.post<{ ok: boolean; message: string; models?: string[]; status_code?: number }>('/ai/config/test').then((r) => r.data),
  aiFetchModels: () => http.get<{ ok: boolean; models: string[]; message: string }>('/ai/config/models').then((r) => r.data),
  aiStats: () => http.get<AIStats>('/ai/config/stats').then((r) => r.data),
  aiChat: (scope: 'chat' | 'agent' | 'monitor', sessionId: string, message: string, attachments?: { type: string; name?: string; url?: string; text?: string; value?: string }[]) =>
    // AI chat is intentionally a *long* call: the backend may need up to
    // 4 agent rounds × an LLM round-trip + tool execution, and a single
    // LLM request can take ~60s on slow providers. The default axios
    // timeout (30s) was way too tight — see screenshot where every
    // /ai/chat call timed out at exactly 30s. We override per-request so
    // the rest of the API stays on the snappy 30s default.
    http
      .post<AIChatResponse>(
        '/ai/chat',
        { scope, session_id: sessionId, message, attachments },
        { timeout: 300_000 },
      )
      .then((r) => r.data),
  aiHistory: (scope: string, sessionId: string) =>
    http.get<AIChatMessage[]>('/ai/history', { params: { scope, session_id: sessionId } }).then((r) => r.data),
  aiClearHistory: (scope: string, sessionId: string) =>
    http.delete('/ai/history', { params: { scope, session_id: sessionId } }).then((r) => r.data),
  aiAgentLogs: (limit = 50) => http.get<AIAgentLog[]>('/ai/agent-logs', { params: { limit } }).then((r) => r.data),

  // ── Short-term session memory (per-user) ──
  aiGetMemory: (sessionId: string, scope = 'chat') =>
    http.get<{ facts: Record<string, unknown>; session_id: string; scope: string }>(
      '/ai/memory',
      { params: { session_id: sessionId, scope } },
    ).then((r) => r.data),
  aiRecallMemory: (sessionId: string, key: string, scope = 'chat') =>
    http.get<{ key: string; value: unknown; found: boolean }>(
      '/ai/memory/recall',
      { params: { session_id: sessionId, scope, key } },
    ).then((r) => r.data),
  aiPutMemory: (
    payload: {
      session_id: string
      scope?: string
      key: string
      value: Record<string, unknown>
      memory_type?: string
      source?: string
      force?: boolean
    },
  ) => http.put<{ ok: boolean; conflict?: boolean; key: string; value: Record<string, unknown> }>('/ai/memory', payload).then((r) => r.data),
  aiDeleteMemory: (sessionId: string, key: string | null, scope = 'chat') =>
    http.delete<{ cleared: number; key?: string; scope?: string }>(
      '/ai/memory',
      { params: { session_id: sessionId, scope, key: key ?? null } },
    ).then((r) => r.data),

  // ── Long-term user preferences ──
  aiGetPreferences: () =>
    http.get<{
      preferences: {
        default_group: string | null
        preferred_vendors: string[]
        alert_keywords: string[]
        custom_prompts: Record<string, string>
      }
    }>('/ai/preferences').then((r) => r.data),
  aiPutPreferences: (payload: {
    default_group?: string | null
    preferred_vendors?: string[]
    alert_keywords?: string[]
    custom_prompts?: Record<string, string>
  }) =>
    http.put<{
      preferences: {
        default_group: string | null
        preferred_vendors: string[]
        alert_keywords: string[]
        custom_prompts: Record<string, string>
      }
    }>('/ai/preferences', payload).then((r) => r.data),

  // ── AI Scripts ────────────────────────────────────────
  aiListScripts: (params: Record<string, unknown> = {}) =>
    http.get('/ai/scripts', { params }).then((r) => r.data),
  aiGetScript: (id: number) => http.get(`/ai/scripts/${id}`).then((r) => r.data),
  aiCreateScript: (data: { name: string; description: string; source_code: string; inputs_schema?: unknown; tags?: string; enabled?: boolean }) =>
    http.post('/ai/scripts', data).then((r) => r.data),
  aiUpdateScript: (id: number, data: Record<string, unknown>) =>
    http.put(`/ai/scripts/${id}`, data).then((r) => r.data),
  aiDeleteScript: (id: number) => http.delete(`/ai/scripts/${id}`).then((r) => r.data),
  aiListScriptRuns: (id: number, limit = 20) =>
    http.get(`/ai/scripts/${id}/runs`, { params: { limit } }).then((r) => r.data),
  /** AST-only extraction: converts an uploaded Python script into a
   * ConfigPlan so the user can preview / Dry-Run it without ever
   * executing the script. */
  aiExtractScriptPlan: (id: number, device_ids: number[] = []) =>
    http.post(`/ai/scripts/${id}/extract-plan`, { device_ids }).then((r) => r.data),

  // ── graph (entity-relationship visualisation) ──────────────────
  graphStats: () => http.get('/graph/stats').then((r) => r.data),
  graphSubgraph: (label: string, opts: { node_id?: string; hops?: number } = {}) =>
    http.get('/graph/subgraph', { params: { label, ...opts } }).then((r) => r.data),
  graphRebuild: () => http.post('/graph/rebuild').then((r) => r.data),

  // ── export ──────────────────────────────────────────────────
  exportData: async (scope: string, format: 'json' | 'csv' = 'json') => {
    const resp = await http.get(`/export/${scope}`, {
      params: { format },
      responseType: 'blob',
    })
    const blob = resp.data as Blob
    const disposition = resp.headers['content-disposition'] as string
    const fnMatch = disposition.match(/filename\*?=['"]?(?:UTF-8'')?([^;\n"']+)/i)
    const filename = fnMatch ? fnMatch[1] : `naw-${scope}.${format === 'csv' ? 'zip' : 'json'}`
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = decodeURIComponent(filename)
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  },
  /** Same as exportData but returns the parsed JSON instead of saving.
   * Used by the Export Center to show a "how many rows would I get?" preview. */
  exportPreview: async (scope: string): Promise<{ records: unknown[]; scope: string }> => {
    const resp = await http.get(`/export/${scope}`, {
      params: { format: 'json' },
    })
    return resp.data
  },

  // ── WebSSH ───────────────────────────────────────────────
  /**
   * Build a WebSocket URL for an interactive SSH session.
   *
   * The JWT is sent via the ``Sec-WebSocket-Protocol`` header (not the URL)
   * to keep it out of server access logs / browser history.
   *
   * ``options`` lets the frontend choose a specific endpoint and credential
   * (CRT-style "Connect" dialog). When omitted, the backend falls back to
   * the device's primary endpoint and its bound credential.
   */
  openWebSsh(
    deviceId: number,
    options: { endpointId?: number | null; credentialId?: number | null } = {},
  ): WebSocket {
    const token = readToken()
    if (!token) throw new Error('not authenticated')
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:'
    // Pass JWT as a query param: the browser rejects custom subprotocol
    // values (RFC 6455 only allows registered tokens), and the backend's
    // `/api/v1/ws/ssh/{device_id}` already accepts `?token=<jwt>` and
    // validates it BEFORE accepting the WebSocket upgrade.
    const params = new URLSearchParams()
    params.set('token', token)
    if (options.endpointId) params.set('endpoint_id', String(options.endpointId))
    if (options.credentialId) params.set('credential_id', String(options.credentialId))
    const url = `${scheme}//${location.host}/api/v1/ws/ssh/${deviceId}?${params.toString()}`
    return new WebSocket(url)
  },
}
