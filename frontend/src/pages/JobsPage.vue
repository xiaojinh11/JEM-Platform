<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref, computed, watch, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'

interface Device { id: number; name: string }
interface JobRow {
  id: number; name: string; kind: string; status: string;
  created_at: string; risk_assessment?: any; rollback_plan?: any;
  ai_comparison?: any; requires_approval?: boolean; approved_by?: string | null;
}
interface HealthSnapshotRow {
  id: number; device_id: number; collected_at: string; vendor: string;
  cpu_pct: number | null; mem_pct: number | null; temp_c: number | null;
  fan_state: string | null; power_w: number | null;
  errors: string[] | null;
}
interface DryRunPreview {
  job_kind: string; estimated_minutes: number; notes: string[];
  risk: {
    is_dangerous: boolean; matched_commands: string[]; warning: string;
    requires_approval: boolean; ai_comment: string | null;
    blast_radius: Record<string, { name: string; neighbours: number[] }>;
  }
  commands_by_device: { device_id: number; commands: string[] }[]
}

const list = ref<JobRow[]>([])
const devices = ref<Device[]>([])
const detail = ref<Record<string, unknown> | null>(null)
const loading = ref(false)
const previewing = ref(false)
const preview = ref<DryRunPreview | null>(null)
const previewError = ref<string | null>(null)
// Stub: the backend exposes POST /api/v1/health/collect but no list
// endpoint yet (see D-3 in COLLECTION_PATTERNS.md). Until that's
// built, we optimistically show the just-triggered task IDs and let
// the user cross-check in the audit page. Showing a "live" list here
// would be a lie.
const lastHealthRun = ref<{ mode: string; devices: number; task_ids: string[]; at: string } | null>(null)
const healthCollecting = ref(false)
const healthSnapshots = ref<HealthSnapshot[]>([])
const healthSnapshotsLoading = ref(false)
const healthSchedules = ref<HealthSchedule[]>([])
const healthSchedulesLoading = ref(false)
const healthExportingId = ref<number | null>(null)
// Snapshot detail page state — mirrors the Python webapp's
// `/view/<rid>` route.  When set, the page swaps its body for a
// full-screen detail view; closing it returns to the snapshot list.
const snapshotDetail = ref<HealthSnapshot | null>(null)
const snapshotDetailOpen = ref(false)
const snapshotDetailLoading = ref<number | null>(null)
// Older snapshots of the same device — powers the "历史版本" link
// row in the detail header (the Python webapp's signature UX).
// We fetch the most recent 5 siblings once the detail loads.
const snapshotHistory = ref<HealthSnapshot[]>([])

/** Map device_id → device name, used in the detail header so the
 * title shows "设备名 · 快照#N" instead of just "#7 · 快照#N".
 * Populated lazily on first detail open. */
const deviceNameById = ref<Record<number, string>>({})
const ensureDeviceNames = async () => {
  if (Object.keys(deviceNameById.value).length) return
  try {
    const list = (await api.listDevices()) as Device[]
    const map: Record<number, string> = {}
    for (const d of list) map[d.id] = d.name
    deviceNameById.value = map
  } catch { /* device list is supplementary */ }
}
const deviceLabel = (id: number) => deviceNameById.value[id] || `#${id}`

const openSnapshotDetail = async (id: number) => {
  snapshotDetailLoading.value = id
  try {
    const detail = (await api.healthSnapshot(id)) as HealthSnapshot
    snapshotDetail.value = detail
    snapshotDetailOpen.value = true
    // Lazily resolve device_id → device name for the header title.
    void ensureDeviceNames()
    // Sibling snapshots = same device_id, different id, newest first.
    // We need at most 5 for the history link bar in the header.
    try {
      const siblings = (await api.healthSnapshots({
        device_id: detail.device_id,
        limit: 6,    // +1 to account for the row we just opened
      })) as HealthSnapshot[]
      snapshotHistory.value = siblings
        .filter((s) => s.id !== detail.id)
        .slice(0, 5)
    } catch { /* history is supplementary — don't block detail */ }
  } catch (e: any) {
    ElMessage.error('加载快照失败：' + (e?.response?.data?.detail || e?.message))
  } finally {
    snapshotDetailLoading.value = null
  }
}

const closeSnapshotDetail = () => {
  snapshotDetailOpen.value = false
  snapshotDetail.value = null
  snapshotHistory.value = []
}

const loadHealthSnapshots = async () => {
  healthSnapshotsLoading.value = true
  try {
    healthSnapshots.value = (await api.healthSnapshots({ limit: 20 })) as HealthSnapshot[]
  } catch { /* silently fail — list is supplementary */ }
  finally { healthSnapshotsLoading.value = false }
}
const loadHealthSchedules = async () => {
  healthSchedulesLoading.value = true
  try {
    healthSchedules.value = (await api.healthSchedules()) as HealthSchedule[]
  } catch { /* silently fail */ }
  finally { healthSchedulesLoading.value = false }
}
const exportSnapshot = async (id: number, format: 'csv' | 'xlsx' | 'txt' = 'csv') => {
  healthExportingId.value = id
  try {
    await api.healthExport(id, format)
    ElMessage.success('导出成功')
  } catch (e: any) {
    ElMessage.error('导出失败：' + (e?.message || '未知错误'))
  } finally { healthExportingId.value = null }
}
const runHealthSchedule = async (id: number) => {
  try {
    await api.healthSchedulesRun(id)
    ElMessage.success('已入队，请稍后在列表查看结果')
    await loadHealthSchedules()
  } catch (e: any) {
    ElMessage.error('触发失败：' + (e?.response?.data?.detail || e?.message))
  }
}
const deleteHealthSchedule = async (id: number) => {
  try {
    await api.healthSchedulesDelete(id)
    ElMessage.success('已删除')
    await loadHealthSchedules()
  } catch (e: any) {
    ElMessage.error('删除失败：' + (e?.response?.data?.detail || e?.message))
  }
}

const form = reactive({
  name: '',
  kind: 'batch_read',
  target_device_ids: [] as number[],
  batch_size: 5,
  // Free-form command list for ``kind=batch_read``.  The form's
  // "需要采集的命令" textarea is bound here and split on newlines
  // before being sent to ``createJob``.  An empty list falls back to
  // the backend's default (``display current-configuration``), so the
  // operator can leave this blank for the simplest case.
  commands_text: 'display current-configuration\ndisplay version',
  params_json: '{}',
  note: '',
})

/** healthMetrics is declared here so the <el-select> in the
 * health panel can bind to it (the old code referenced it in the
 * template without ever declaring it, which caused Vue 3 runtime
 * warnings and left the select uncontrolled).  Empty array =
 * collect all metrics (backend default). */
const healthMetrics = ref<string[]>([])

const refresh = async () => {
  loading.value = true
  try {
    list.value = (await api.listJobs()) as JobRow[]
    devices.value = (await api.listDevices()) as Device[]
  } finally {
    loading.value = false
  }
}

// AI Plan hydration (from AI Assistant page "Dry-Run this AI plan" link)
const route = useRoute()
const hydrateFromAiPlan = async () => {
  const b64 = route.query.ai_plan_b64
  if (!b64 || typeof b64 !== 'string') return
  try {
    const json = decodeURIComponent(escape(atob(b64)))
    const plan = JSON.parse(json)
    if (!plan?.commands?.length) return
    form.kind = 'batch_write'
    form.params_json = JSON.stringify(
      { __inline_commands: plan.commands, __plan_source: plan.source || 'ai_chat' },
      null, 2,
    )
    if (!devices.value.length) {
      try { devices.value = (await api.listDevices()) as Device[] } catch { /* leave empty */ }
    }
    ElMessage.success(`已加载 AI 计划（${plan.commands.length} 条命令）。请选择目标设备后点 Dry-Run 预览。`)
  } catch (err) {
    ElMessage.error('AI 计划解码失败：' + (err as Error).message)
  }
}
watch(() => route.query.ai_plan_b64, hydrateFromAiPlan, { immediate: true })

const parsedParams = computed<Record<string, unknown> | null>(() => {
  if (form.kind === 'batch_read') return null
  try { return JSON.parse(form.params_json || '{}') }
  catch { return null }
})

const dryRun = async () => {
  if (form.kind === 'batch_read') {
    ElMessage.info('只读任务无需 dry-run')
    return
  }
  if (form.target_device_ids.length === 0) {
    ElMessage.warning('请先选择目标设备')
    return
  }
  previewError.value = null
  previewing.value = true
  try {
    const params = parsedParams.value
    if (params === null) {
      ElMessage.error('参数 JSON 格式错误')
      return
    }
    preview.value = await api.dryRunJob({
      commands: form.kind === 'batch_write' ? ['display this'] : [],
      params,
      device_ids: form.target_device_ids,
    })
  } catch (e: any) {
    previewError.value = e?.response?.data?.detail || String(e)
  } finally {
    previewing.value = false
  }
}

const submit = async () => {
  if (!form.name) { ElMessage.warning('请填写任务名称'); return }
  if (!form.target_device_ids.length) { ElMessage.warning('请选择至少一台设备'); return }
  const params = parsedParams.value
  if (form.kind !== 'batch_read' && params === null) {
    ElMessage.error('参数 JSON 格式错误')
    return
  }
  // Split the textarea on newlines, drop blanks.  For kind=batch_read
  // the backend has a fallback default if this is empty, so the
  // operator can leave the textarea blank for the simple case.
  const commands = form.kind === 'batch_read'
    ? form.commands_text.split(/\r?\n/).map(s => s.trim()).filter(Boolean)
    : undefined
  // Guard: if the operator explicitly cleared the textarea and submitted,
  // surface a warning before we let the backend silently substitute the
  // hardcoded default.  The operator needs to know their blank input
  // became "display current-configuration" rather than wondering why
  // their custom commands didn't run.
  if (form.kind === 'batch_read' && !commands.length && form.commands_text.trim()) {
    ElMessage.warning('命令列表为空，将使用默认命令「display current-configuration」')
  }
  await api.createJob({
    name: form.name,
    kind: form.kind,
    target_device_ids: form.target_device_ids,
    batch_size: form.batch_size,
    note: form.note || undefined,
    params: params ?? undefined,
    commands,
  })
  ElMessage.success(`任务「${form.name}」已创建`)
  form.name = ''
  refresh()
}

const start = async (id: number) => {
  try {
    await ElMessageBox.confirm('确认启动该任务？', '提示', { type: 'warning' })
  } catch { return }
  try {
    await api.startJob(id)
    ElMessage.success('任务已入队')
    await refresh()
  } catch (e: any) {
    ElMessage.error('启动失败：' + (e?.response?.data?.detail || e?.message || '未知错误'))
  }
}

const cancel = async (id: number) => {
  try {
    await api.cancelJob(id)
    ElMessage.success('已请求取消')
    await refresh()
  } catch (e: any) {
    ElMessage.error('取消失败：' + (e?.response?.data?.detail || e?.message || '未知错误'))
  }
}

const remove = async (id: number, name: string) => {
  try {
    await ElMessageBox.confirm(
      `确认删除任务「${name}」？此操作不可撤销，关联的执行步骤会一并删除。`,
      '删除任务',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    // User hit 取消 — ElMessageBox.confirm throws on dismissal.
    // Don't confuse that with an API failure.
    return
  }
  try {
    await api.deleteJob(id)
    ElMessage.success('已删除')
    // If the detail panel is showing this job, close it so the
    // operator doesn't see a "stale" job they're not allowed to.
    if (detail.value && (detail.value as { id: number }).id === id) {
      detail.value = null
    }
    await refresh()
  } catch (e: any) {
    ElMessage.error('删除失败：' + (e?.response?.data?.detail || e?.message || '未知错误'))
  }
}

const show = async (id: number) => {
  try {
    const r = await api.getJob(id)
    detail.value = r as Record<string, unknown>
    // Scroll the detail panel into view on narrow screens where it
    // lives below the table.  Without this, clicking "详情" leaves
    // the operator staring at the same spot in the table and not
    // noticing the panel that just appeared at the bottom.
    nextTick(() => {
      const el = document.querySelector('.detail-panel')
      if (el) (el as HTMLElement).scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    })
  } catch (e: any) {
    ElMessage.error('加载任务详情失败：' + (e?.response?.data?.detail || e?.message || '未知错误'))
  }
}

const fetchRollback = async (id: number) => {
  try {
    const plan = await api.rollbackPlan(id)
    await ElMessageBox.alert(formatJson(plan), '回滚计划', { confirmButtonText: '关闭' })
  } catch (e: any) {
    if (e === 'cancel' || e?.message === 'cancel') return
    ElMessage.error('回滚计划加载失败：' + (e?.response?.data?.detail || e?.message || '未知错误'))
  }
}

const fetchAiCompare = async (id: number) => {
  try {
    const comp = await api.aiCompareJob(id)
    await ElMessageBox.alert(formatJson(comp), 'AI 对比报告', { confirmButtonText: '关闭' })
  } catch (e: any) {
    if (e === 'cancel' || e?.message === 'cancel') return
    ElMessage.error('AI 对比失败：' + (e?.response?.data?.detail || e?.message || '未知错误'))
  }
}

// Health collection: read-only, idempotent, so we let operator/admin
// fire it without an approval gate.  We use a separate confirm step
// because the "all devices" mode can take a few minutes on a 500-device
// fleet and the operator should know what they're kicking off.
const collectHealth = async (mode: 'all' | 'selected') => {
  if (mode === 'selected' && !form.target_device_ids.length) {
    ElMessage.warning('请先在右侧表单选择目标设备')
    return
  }
  await ElMessageBox.confirm(
    mode === 'all'
      ? '将对全部设备发起健康采集（CPU/内存/温度/风扇/电源），通常需要 1-3 分钟。'
      : `将对所选 ${form.target_device_ids.length} 台设备发起健康采集，是否继续？`,
    '健康采集',
    { type: 'warning', confirmButtonText: '开始采集' },
  )
  healthCollecting.value = true
  try {
    const resp = await api.healthCollect(
      mode === 'all' ? undefined : form.target_device_ids,
      // Forward the metric selection so the backend only collects what
      // the operator asked for.  The old code dropped this on the
      // floor, so the select was visible but did nothing.
      healthMetrics.value?.length ? healthMetrics.value : undefined,
    )
    lastHealthRun.value = {
      mode: resp.mode,
      devices: resp.devices || form.target_device_ids.length,
      task_ids: resp.task_ids,
      at: new Date().toISOString(),
    }
    ElMessage.success(`已入队 ${resp.task_ids.length} 个健康采集任务（${resp.mode}）`)
    // Force an immediate refresh so the "暂无快照" message disappears
    // the moment the first row lands (rather than waiting 3s for the
    // poller's first tick).  Real SSH collection can take 1-3 min for
    // 500 devices; the poller below keeps refreshing the table until
    // either all the queue drains or 30s elapse.
    try { await loadHealthSnapshots() } catch { /* ignore */ }
    let polls = 0
    const poller = window.setInterval(async () => {
      polls += 1
      try { await loadHealthSnapshots() } catch { /* ignore */ }
      if (polls >= 10) window.clearInterval(poller)
    }, 3000)
  } catch (e: any) {
    ElMessage.error('健康采集提交失败：' + (e?.response?.data?.detail || e?.message))
  } finally {
    healthCollecting.value = false
  }
}

const formatJson = (obj: unknown) => {
  try { return JSON.stringify(obj, null, 2) } catch { return String(obj) }
}

const statusTag = (s: string) => {
  if (s === 'succeeded') return 'success'
  if (s === 'failed') return 'danger'
  if (s === 'needs_verification') return 'warning'
  if (s === 'running') return 'primary'
  return 'info'
}

const kindLabel = (k: string) => {
  if (k === 'batch_read') return '批量采集'
  if (k === 'batch_write') return '批量下发'
  if (k === 'template_apply') return '模板应用'
  return k
}

const riskLevel = (row: JobRow): { label: string; tone: string } => {
  const level = row.risk_assessment?.level
  if (level === 'dangerous') return { label: '高危', tone: 'danger' }
  if (row.requires_approval) return { label: '需审批', tone: 'warning' }
  return { label: '安全', tone: 'success' }
}

onMounted(() => {
  refresh()
  loadHealthSnapshots()
  loadHealthSchedules()

  // Polling: without this the job list only ever shows the moment the
  // user clicked "启动".  Celery dispatches batch_collect to the
  // worker; the worker's step_update / status_update events flow over
  // the per-job WebSocket (JobProgressViewer), but the *list* page
  // has no per-job WS — it gets its data from the periodic
  // active-jobs / jobs/ endpoints.  Re-poll aggressively while any
  // task is still in flight (so an operator sees status flip from
  // running → succeeded within a couple of seconds), then slow
  // down to 8s once the queue is quiet.
  let pollTimer: number | null = null
  const tick = async () => {
    const anyInFlight = list.value.some(
      (j) => j.status === 'running' || j.status === 'pending' || j.status === 'draft',
    )
    await refresh()
    pollTimer = window.setTimeout(tick, anyInFlight ? 3000 : 8000)
  }
  pollTimer = window.setTimeout(tick, 3000)
  onBeforeUnmount(() => {
    if (pollTimer) window.clearTimeout(pollTimer)
  })
})
</script>

<template>
  <div class="page" v-loading="loading">

    <div class="page-grid">
      <!-- 左侧：任务列表 -->
      <div class="panel">
        <div class="panel-header">
          <span>任务列表</span>
          <el-button size="small" @click="refresh">刷新</el-button>
        </div>
        <el-table :data="list" border stripe size="small">
          <el-table-column type="index" label="#" width="50" align="center" />
          <el-table-column prop="name" label="名称" min-width="130" show-overflow-tooltip />
          <el-table-column label="类型" width="100">
            <template #default="{ row }">
              <el-tag size="small" effect="plain">{{ kindLabel(String((row as JobRow).kind)) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="100" align="center">
            <template #default="{ row }">
              <el-tag size="small" :type="statusTag(String((row as JobRow).status))" effect="dark">
                {{ (row as JobRow).status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="AI 风险" width="80" align="center">
            <template #default="{ row }">
              <el-tag size="small" :type="riskLevel(row as JobRow).tone as any" effect="plain">
                {{ riskLevel(row as JobRow).label }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="created_at" label="创建时间" min-width="140" show-overflow-tooltip />
          <el-table-column label="操作" width="320" align="center">
            <template #default="{ row }">
              <el-button size="small" link @click="show((row as JobRow).id)">详情</el-button>
              <el-button size="small" link type="primary"
                :disabled="!['draft','pending','canceled'].includes(String((row as JobRow).status))"
                @click="start((row as JobRow).id)">启动</el-button>
              <el-button size="small" link type="info"
                :disabled="(row as JobRow).status !== 'succeeded'"
                @click="fetchAiCompare((row as JobRow).id)">AI 对比</el-button>
              <el-button size="small" link type="danger"
                :disabled="['succeeded','failed','partial','canceled','needs_verification'].includes(String((row as JobRow).status))"
                @click="cancel((row as JobRow).id)">取消</el-button>
              <el-button size="small" link type="danger"
                :disabled="['running','pending'].includes(String((row as JobRow).status))"
                @click="remove((row as JobRow).id, (row as JobRow).name)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 右栏：健康采集 + 创建任务 -->
      <div class="health-column">
        <!-- 健康采集面板 -->
        <div class="panel">
          <div class="panel-header">
            <span>健康采集</span>
            <el-tag size="small" effect="plain">v1.7 / 只读</el-tag>
          </div>
          <div class="form-body">
            <div class="form-field">
              <label>指标（多选）</label>
              <el-select v-model="healthMetrics" multiple collapse-tags style="width:100%" placeholder="全选即采集全部">
                <el-option label="CPU 使用率" value="cpu" />
                <el-option label="内存" value="mem" />
                <el-option label="温度" value="temp" />
                <el-option label="风扇" value="fan" />
                <el-option label="功率" value="power" />
                <el-option label="收发光" value="optical" />
                <el-option label="配置指纹" value="config" />
              </el-select>
            </div>
            <div class="health-buttons">
              <el-button type="primary" size="small" :loading="healthCollecting"
                @click="collectHealth('all')">采集全部</el-button>
              <el-button size="small" :loading="healthCollecting"
                :disabled="!form.target_device_ids.length"
                @click="collectHealth('selected')">采集所选</el-button>
              <el-button size="small" text @click="loadHealthSnapshots()">刷新列表</el-button>
            </div>
            <div v-if="lastHealthRun" class="health-last-run">
              <div class="preview-row">
                <span>最近一次</span>
                <b>{{ lastHealthRun.mode === 'all' ? '全量' : '所选' }} · {{ lastHealthRun.devices }} 台</b>
              </div>
              <div class="preview-row">
                <span>任务 ID</span>
                <code class="task-id-list">
                  <span v-for="(tid, i) in lastHealthRun.task_ids" :key="tid">
                    {{ tid }}<span v-if="i < lastHealthRun.task_ids.length - 1">, </span>
                  </span>
                </code>
              </div>
              <div class="preview-row">
                <span>提交时间</span>
                <span>{{ new Date(lastHealthRun.at).toLocaleString() }}</span>
              </div>
            </div>
          </div>

          <!-- 最近快照表（紧凑） -->
          <div class="preview-box">
            <div class="preview-header">
              <span>最近快照（{{ healthSnapshots.length }}）</span>
              <span class="health-hint">点击导出按钮保存为 CSV/XLSX/TXT</span>
            </div>
            <div v-if="healthSnapshotsLoading" class="health-hint">加载中…</div>
            <div v-else-if="healthSnapshots.length" class="health-snap-table-wrap">
              <table class="health-snap-table">
                <colgroup>
                  <col class="col-device" />
                  <col class="col-time" />
                  <col class="col-num" />
                  <col class="col-num" />
                  <col class="col-num" />
                  <col class="col-num" />
                  <col class="col-status" />
                  <col class="col-actions" />
                </colgroup>
                <thead>
                  <tr>
                    <th class="col-device">设备</th>
                    <th class="col-time">采集时间</th>
                    <th class="col-num">CPU</th>
                    <th class="col-num">内存</th>
                    <th class="col-num">温度</th>
                    <th class="col-num">功率</th>
                    <th class="col-status">状态</th>
                    <th class="col-actions">操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="s in healthSnapshots" :key="s.id" :class="{ 'snap-row-error': s.errors?.length }">
                    <td class="col-device">#{{ s.device_id }}</td>
                    <td class="col-time">{{ new Date(s.collected_at).toLocaleString() }}</td>
                    <td class="col-num">{{ s.cpu_pct != null ? s.cpu_pct.toFixed(1) + '%' : '—' }}</td>
                    <td class="col-num">{{ s.mem_pct != null ? s.mem_pct.toFixed(1) + '%' : '—' }}</td>
                    <td class="col-num">{{ s.temp_c != null ? s.temp_c.toFixed(0) + '℃' : '—' }}</td>
                    <td class="col-num">{{ s.power_w != null ? s.power_w.toFixed(0) + 'W' : '—' }}</td>
                    <td class="col-status">
                      <el-tag v-if="s.errors?.length" type="danger" size="small"
                        :title="s.errors.join('\n')">失败</el-tag>
                      <el-tooltip v-else-if="s.source === 'simulated'" content="模拟设备生成的合成数据，非真实采集" placement="top">
                        <el-tag type="warning" size="small">模拟</el-tag>
                      </el-tooltip>
                      <el-tag v-else type="success" size="small">成功</el-tag>
                    </td>
                    <td class="col-actions">
                      <div class="snap-row-actions">
                        <el-button size="small" :loading="snapshotDetailLoading === s.id"
                          @click="openSnapshotDetail(s.id)">查看</el-button>
                        <el-button size="small" :loading="healthExportingId === s.id"
                          @click="exportSnapshot(s.id, 'csv')">CSV</el-button>
                        <el-button size="small" :loading="healthExportingId === s.id"
                          @click="exportSnapshot(s.id, 'xlsx')">XLSX</el-button>
                        <el-button size="small" :loading="healthExportingId === s.id"
                          @click="exportSnapshot(s.id, 'txt')">TXT</el-button>
                      </div>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-else class="health-hint">暂无快照。点击「采集全部」开始第一次采集。</div>
          </div>
        </div>

        <!-- 采集计划面板 -->
        <div class="panel">
          <div class="panel-header">
            <span>采集计划</span>
            <el-tag size="small" effect="plain">{{ healthSchedules.length }} 个</el-tag>
          </div>
          <div v-if="healthSchedulesLoading" class="form-body health-hint">加载中…</div>
          <div v-else-if="!healthSchedules.length" class="form-body health-hint">
            暂无计划。可在下方"创建任务"里手动采集，或扩展此页支持计划创建。
          </div>
          <div v-else class="form-body">
            <div v-for="sch in healthSchedules" :key="sch.id" class="schedule-row">
              <div class="schedule-head">
                <b>{{ sch.name }}</b>
                <el-tag size="small" :type="sch.enabled ? 'success' : 'info'">
                  {{ sch.enabled ? '启用' : '停用' }} · {{ sch.interval_minutes }} 分钟
                </el-tag>
              </div>
              <div class="schedule-meta health-hint">
                设备: {{ sch.device_ids.length || '全部' }} ·
                指标: {{ sch.metrics.join(', ') || '全部' }}
              </div>
              <div class="schedule-actions">
                <el-button size="small" link type="primary"
                  :disabled="!sch.enabled" @click="runHealthSchedule(sch.id)">立即执行</el-button>
                <el-button size="small" link type="danger"
                  @click="deleteHealthSchedule(sch.id)">删除</el-button>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 中栏：创建任务 -->
      <div class="panel">
        <div class="panel-header">
          <span>创建任务</span>
        </div>
        <div class="form-body">
          <div class="form-field">
            <label>任务名称</label>
            <el-input v-model="form.name" placeholder="输入任务名称" />
          </div>
          <div class="form-field">
            <label>任务类型</label>
            <el-select v-model="form.kind" style="width:100%">
              <el-option value="batch_read" label="批量采集（只读）" />
              <el-option value="batch_write" label="批量下发（带备份）" />
              <el-option value="template_apply" label="模板应用" />
            </el-select>
          </div>
          <div class="form-field">
            <label>目标设备</label>
            <el-select v-model="form.target_device_ids" multiple filterable collapse-tags style="width:100%">
              <el-option
                v-for="d in devices" :key="(d as Device).id"
                :value="(d as Device).id"
                :label="(d as Device).name"
              />
            </el-select>
          </div>
          <div class="form-field">
            <label>每批大小</label>
            <el-input-number v-model="form.batch_size" :min="1" :max="50" />
          </div>
          <div class="form-field" v-if="form.kind === 'batch_read'">
            <label>需要采集的命令</label>
            <el-input
              v-model="form.commands_text"
              type="textarea"
              :autosize="{ minRows: 5, maxRows: 12 }"
              placeholder="每行一条命令，例如：&#10;display current-configuration&#10;display version&#10;display ip interface brief"
            />
            <div class="form-hint">
              留空则使用默认命令（display current-configuration）。
              危险命令（reboot/erase/format 等）会被命令校验拒绝。
            </div>
          </div>
          <div class="form-field" v-if="form.kind !== 'batch_read'">
            <label>参数 JSON</label>
            <el-input v-model="form.params_json" type="textarea" :autosize="{minRows:3}" placeholder='{"key":"value"}' />
          </div>
          <div class="form-field">
            <label>备注</label>
            <el-input v-model="form.note" placeholder="可选" />
          </div>
          <div class="btn-row">
            <el-button @click="dryRun" :loading="previewing" :disabled="form.kind === 'batch_read'">
              Dry-Run 预览
            </el-button>
            <el-button type="primary" @click="submit">创建任务</el-button>
          </div>
        </div>

        <!-- Dry-run 预览结果 -->
        <div v-if="preview || previewError" class="preview-box">
          <div class="preview-header">
            <span>Dry-Run 预览</span>
            <el-button size="small" text @click="preview = null; previewError = null">关闭</el-button>
          </div>
          <div v-if="previewError" class="preview-error">{{ previewError }}</div>
          <div v-else-if="preview">
            <div class="preview-row">
              <span>预计耗时</span>
              <b>{{ preview.estimated_minutes }} 分钟</b>
            </div>
            <div class="preview-row">
              <span>高危命令</span>
              <el-tag v-if="preview.risk.is_dangerous" type="danger" size="small">
                {{ preview.risk.matched_commands.length }} 条
              </el-tag>
              <el-tag v-else type="success" size="small">无</el-tag>
            </div>
            <div v-if="preview.risk.ai_comment" class="ai-comment">
              <b>AI 提示：</b>{{ preview.risk.ai_comment }}
            </div>
            <div v-if="preview.notes.length" class="notes">
              <div v-for="(n, i) in preview.notes" :key="i" class="note">• {{ n }}</div>
            </div>
            <details v-if="preview.risk.matched_commands.length">
              <summary>高危命令清单</summary>
              <pre class="cmd-list">{{ preview.risk.matched_commands.join('\n') }}</pre>
            </details>
          </div>
        </div>
      </div>
    </div>

    <!-- 快照详情：弹层展示,不再依赖流式布局 -->
    <el-dialog
      v-model="snapshotDetailOpen"
      :title="snapshotDetail ? `${deviceLabel(snapshotDetail.device_id)} · 快照 #${snapshotDetail.id}` : '快照详情'"
      width="880px"
      top="6vh"
      :close-on-click-modal="true"
      :close-on-press-escape="true"
      class="snap-dialog"
      @close="closeSnapshotDetail"
    >
      <section v-if="snapshotDetail" class="snap-view">
        <div class="snap-view-bar">
          <el-button size="small" :loading="healthExportingId === snapshotDetail.id"
            @click="exportSnapshot(snapshotDetail.id, 'txt')">下载本条 (TXT)</el-button>
          <el-dropdown trigger="click" @command="(fmt: any) => exportSnapshot(snapshotDetail!.id, fmt)">
            <el-button size="small">下载为…</el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="csv">CSV</el-dropdown-item>
                <el-dropdown-item command="xlsx">XLSX</el-dropdown-item>
                <el-dropdown-item command="txt">TXT</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>

        <p class="snap-view-meta">
          <span>采集时间：{{ new Date(snapshotDetail.collected_at).toLocaleString() }}</span>
          <span> · </span>
          <span>厂商：{{ snapshotDetail.vendor || '—' }}</span>
          <span> · </span>
          <span>来源：{{ snapshotDetail.source }}</span>
          <span v-if="snapshotDetail.metrics_run?.length"> · 指标：{{ snapshotDetail.metrics_run.join('、') }}</span>
          <span v-if="snapshotDetail.config_lines != null"> · {{ snapshotDetail.config_lines }} 行</span>
          <span v-if="snapshotDetail.config_fingerprint"> · 指纹 {{ snapshotDetail.config_fingerprint }}</span>
        </p>
        <p v-if="snapshotHistory.length" class="snap-view-history">
          <span class="muted">历史版本：</span>
          <a v-for="h in snapshotHistory" :key="h.id" class="history-link"
            @click="openSnapshotDetail(h.id)">
            {{ new Date(h.collected_at).toLocaleString() }} (#{{ h.id }})
          </a>
        </p>

        <div class="snap-cards">
          <div class="snap-card"><span>CPU</span><b>{{ snapshotDetail.cpu_pct != null ? snapshotDetail.cpu_pct.toFixed(1) + '%' : '—' }}</b></div>
          <div class="snap-card"><span>内存</span><b>{{ snapshotDetail.mem_pct != null ? snapshotDetail.mem_pct.toFixed(1) + '%' : '—' }}</b></div>
          <div class="snap-card"><span>温度</span><b>{{ snapshotDetail.temp_c != null ? snapshotDetail.temp_c.toFixed(0) + '℃' : '—' }}</b></div>
          <div class="snap-card"><span>风扇</span><b>{{ snapshotDetail.fan_state ?? '—' }}</b></div>
          <div class="snap-card"><span>功率</span><b>{{ snapshotDetail.power_w != null ? snapshotDetail.power_w.toFixed(0) + 'W' : '—' }}</b></div>
        </div>

        <div v-if="snapshotDetail.errors?.length" class="snap-errors-block">
          <b>采集错误</b>
          <ul>
            <li v-for="(e, i) in snapshotDetail.errors" :key="i">{{ e }}</li>
          </ul>
        </div>

        <h3 v-if="snapshotDetail.optical?.length" class="snap-h3">
          光模块（{{ snapshotDetail.optical.length }} 端口）
        </h3>
        <table v-if="snapshotDetail.optical?.length" class="snap-table">
          <thead>
            <tr><th>端口</th><th>收光 (dBm)</th><th>发光 (dBm)</th></tr>
          </thead>
          <tbody>
            <tr v-for="(p, i) in snapshotDetail.optical" :key="i">
              <td>{{ p.port }}</td>
              <td>{{ p.rx_dbm != null ? p.rx_dbm.toFixed(2) : '—' }}</td>
              <td>{{ p.tx_dbm != null ? p.tx_dbm.toFixed(2) : '—' }}</td>
            </tr>
          </tbody>
        </table>

        <h3 class="snap-h3">配置摘录</h3>
        <pre v-if="snapshotDetail.config_excerpt" class="snap-pre">{{ snapshotDetail.config_excerpt }}</pre>
        <p v-else class="muted">本次采集未抓取配置（metrics_run 不含 config）。</p>

        <h3 class="snap-h3">原始 SSH 输出摘录</h3>
        <pre v-if="snapshotDetail.raw_excerpt" class="snap-pre">{{ snapshotDetail.raw_excerpt }}</pre>
        <p v-else class="muted">本次采集未保存原始输出。</p>
      </section>
    </el-dialog>

    <!-- 详情 -->
    <div v-if="detail" class="detail-panel">
      <div class="panel-header">
        <span>任务详情 · {{ (detail as {name:string}).name }}</span>
        <el-button size="small" text @click="detail = null">关闭</el-button>
      </div>
      <pre class="detail-json">{{ JSON.stringify(detail, null, 2) }}</pre>
    </div>

  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.page-grid {
  display: grid;
  /* Three columns:
   *   1fr       — task list (left, wide)
   *   360px     — health + schedule panels (middle, compact)
   *   340px     — create-job form (right, compact)
   * On narrow screens (<1100px) we collapse to a single column
   * so the panels stack vertically instead of overflowing.
   */
  grid-template-columns: minmax(0, 1fr) 360px 340px;
  gap: 16px;
}
@media (max-width: 1100px) {
  .page-grid { grid-template-columns: 1fr; }
}

.panel {
  background: var(--bg-canvas);
  border-radius: var(--radius-md);
  border: 1px solid var(--border-1);
  display: flex;
  flex-direction: column;
  color: var(--text-1);
}

.panel-header {
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-1);
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-1);
}

.form-body {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.form-field { display: flex; flex-direction: column; gap: 5px; }
.form-field label { font-size: 12px; font-weight: 500; color: var(--text-3); }
.form-hint { font-size: 11px; color: var(--text-3); line-height: 1.4; margin-top: 2px; }
.btn-row { display: flex; gap: 8px; margin-top: 4px; }

.preview-box {
  border-top: 1px solid var(--border-1);
  padding: 12px 16px;
  background: var(--bg-code);
}
.preview-header {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 10px; font-size: 12px; font-weight: 600;
}
.preview-row {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 8px; font-size: 12px;
}
.preview-row > span { color: var(--text-3); }
.preview-error { color: var(--el-color-danger); font-size: 12px; }
.ai-comment {
  background: var(--bg-canvas);
  border-left: 3px solid var(--el-color-primary);
  padding: 8px 10px;
  margin: 8px 0;
  font-size: 12px;
  line-height: 1.5;
  border-radius: 4px;
}
.notes { display: flex; flex-direction: column; gap: 3px; margin: 6px 0; }
.note { font-size: 11px; color: var(--text-3); }
.cmd-list {
  margin: 6px 0 0; padding: 8px; font-size: 11px;
  background: var(--bg-canvas); border-radius: 4px;
  max-height: 120px; overflow: auto; font-family: var(--font-mono);
}

.detail-panel {
  background: var(--bg-canvas);
  border-radius: var(--radius-md);
  border: 1px solid var(--border-1);
}
.detail-json {
  margin: 0;
  padding: 16px;
  font-size: 12px;
  line-height: 1.6;
  background: var(--bg-code);
  color: var(--text-1);
  max-height: 400px;
  overflow: auto;
  font-family: var(--font-mono);
  border-radius: var(--radius-md);
}

.health-buttons { display: flex; gap: 8px; flex-wrap: wrap; }
.health-last-run { border-top: 1px solid var(--border-1); padding-top: 10px; margin-top: 4px; }
.task-id-list {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-3);
  word-break: break-all;
}
.health-hint { color: var(--text-3); font-size: 11px; }
.health-hint code { font-size: 10px; }

.health-buttons { display: flex; gap: 8px; flex-wrap: wrap; }
.health-last-run { border-top: 1px solid var(--border-1); padding-top: 10px; margin-top: 4px; }
.task-id-list {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-3);
  word-break: break-all;
}
.health-hint { color: var(--text-3); font-size: 11px; }
.health-hint code { font-size: 10px; }

/* v1.7 health column + compact snapshot table + schedule list */
.health-column {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;  /* allow children to shrink inside grid */
}
/* 健康快照表 — 横向可滚动容器,操作列粘在右侧,窄屏不会把按钮挤出去 */
.health-snap-table-wrap {
  position: relative;
  width: 100%;
  max-width: 100%;
  overflow-x: auto;
  overflow-y: hidden;
  -webkit-overflow-scrolling: touch;
  /* 阴影提示可以横向滑动 */
  background:
    linear-gradient(to right, var(--bg-panel) 30%, rgba(0,0,0,0)),
    linear-gradient(to right, rgba(0,0,0,0), var(--bg-panel) 70%) right top,
    radial-gradient(farthest-side at 0 50%, rgba(0,0,0,0.15), rgba(0,0,0,0)),
    radial-gradient(farthest-side at 100% 50%, rgba(0,0,0,0.15), rgba(0,0,0,0)) right top;
  background-repeat: no-repeat;
  background-color: var(--bg-panel);
  background-size: 20px 100%, 20px 100%, 8px 100%, 8px 100%;
  background-attachment: local, local, scroll, scroll;
}
.health-snap-table {
  width: 100%;
  min-width: 720px;          /* 触发容器横向滚动;低于这个宽度就让用户滑 */
  border-collapse: collapse;
  font-size: 11px;
  table-layout: auto;         /* 内容自适应,不再死列宽 */
}
.health-snap-table th,
.health-snap-table td {
  text-align: left;
  padding: 5px 8px;
  border-bottom: 1px solid var(--border-1);
  white-space: nowrap;
}
.health-snap-table th {
  font-weight: 600;
  color: var(--text-3);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  background: var(--bg-code);
}
.health-snap-table tr:hover td {
  background: var(--bg-code);
}
/* 操作列粘右侧,hover 时仍需维持面板色,否则会被透出 */
.health-snap-table tr:hover .col-actions,
.health-snap-table tr:hover th.col-actions {
  background: var(--bg-code);
}
.health-snap-table tr.snap-row-error td {
  background: rgba(245, 108, 108, 0.05);
  color: var(--text-3);
}
.health-snap-table tr.snap-row-error:hover td {
  background: rgba(245, 108, 108, 0.12);
}
.health-snap-table tr.snap-row-error:hover .col-actions,
.health-snap-table tr.snap-row-error:hover th.col-actions {
  background: rgba(245, 108, 108, 0.12);
}
.health-snap-table .col-device { min-width: 56px; }
.health-snap-table .col-time   { min-width: 150px; }
.health-snap-table .col-num    { min-width: 64px; text-align: right; }
.health-snap-table .col-status { min-width: 60px; text-align: center; }
.health-snap-table .col-status .el-tag { transform: scale(0.85); }
/* 操作列粘在表格右侧:列表横向滚动时,按钮始终可见 */
.health-snap-table .col-actions,
.health-snap-table th.col-actions {
  position: sticky;
  right: 0;
  z-index: 2;
  min-width: 240px;
  background: var(--bg-panel);
  white-space: nowrap;
  text-align: right;
  padding-right: 8px !important;
  box-shadow: -6px 0 6px -4px rgba(0, 0, 0, 0.12);
}
.health-snap-table thead th.col-actions {
  z-index: 3;          /* 表头层叠在表体之上 */
}
/* Plain ``<div>`` flex container — no Element Plus wrapping that
 * might inject extra nodes that override our width.  ``!important``
 * is overkill but cheap insurance against the global ``.el-button``
 * rule winning. */
.health-snap-table .col-actions .snap-row-actions {
  display: inline-flex !important;
  flex-direction: row !important;
  flex-wrap: nowrap !important;
  align-items: center !important;
  gap: 4px !important;
  vertical-align: middle !important;
  width: auto !important;
}
.health-snap-table .col-actions .snap-row-actions :deep(.el-button) {
  padding: 4px 8px !important;
  margin: 0 !important;
  display: inline-block !important;
  width: auto !important;
  vertical-align: middle !important;
}
.schedule-row {
  padding: 8px 0;
  border-bottom: 1px solid var(--border-1);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.schedule-row:last-child { border-bottom: 0; }
.schedule-head {
  display: flex; justify-content: space-between; align-items: center; gap: 6px;
}
.schedule-head b { font-size: 12px; }
.schedule-meta { line-height: 1.4; }
.schedule-actions { display: flex; gap: 4px; }

/* snapshot detail dialog */
.snapshot-detail { font-size: 13px; }
.snap-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px 16px;
  margin-bottom: 12px;
}
.snap-grid > div { display: flex; justify-content: space-between; gap: 8px; padding: 4px 0; border-bottom: 1px dashed var(--border-1); }
.snap-grid > div span { color: var(--text-3); }
.snap-block { margin-top: 12px; }
.snap-block h4 { margin: 0 0 6px; font-size: 12px; color: var(--text-3); font-weight: 600; }
.snap-errors { background: rgba(245, 108, 108, 0.08); padding: 8px 12px; border-radius: 4px; margin-top: 8px; }
.snap-errors h4 { color: var(--el-color-danger); }
.snap-errors ul { margin: 4px 0 0 16px; padding: 0; }
.snap-pre {
  margin: 6px 0 0;
  padding: 8px;
  background: var(--bg-code);
  font-size: 11px;
  max-height: 240px;
  overflow: auto;
  font-family: var(--font-mono);
  border-radius: 4px;
  white-space: pre-wrap;
}
.optical-table { width: 100%; border-collapse: collapse; font-size: 11px; margin-top: 4px; }
.optical-table th, .optical-table td { padding: 4px 8px; border-bottom: 1px solid var(--border-1); text-align: left; }
.optical-table th { color: var(--text-3); font-weight: 600; }

/* ── Python-webapp-style snapshot detail view ─────────────────
 * Reproduces the layout of /view/<rid> from D:\PythonProject5\webapp.py:
 *   - top action bar (back / download)
 *   - H2 title   "设备名 · 快照#N"
 *   - meta line  "采集时间 | 厂商 | 来源 | 指标 | …"
 *   - history row "历史版本: time#1, time#2, …"
 *   - cards row for headline metrics
 *   - <pre> blocks for config_excerpt / raw_excerpt
 */
.snap-view {
  /* 弹层里用,去掉外边距和边框(由 el-dialog 自身提供) */
  margin-top: 0;
  border: none;
  background: transparent;
  padding: 0;
  color: var(--text-1);
}
.snap-view-bar {
  display: flex; gap: 8px; align-items: center; margin-bottom: 12px;
}
.snap-view-title {
  font-size: 18px; font-weight: 600; margin: 0 0 6px;
  color: var(--text-1);
}
.snap-view-meta {
  margin: 0 0 6px; font-size: 13px; color: var(--text-3);
}
.snap-view-history { margin: 0 0 14px; font-size: 12px; }
.snap-view-history .muted { color: var(--text-3); margin-right: 6px; }
.history-link {
  display: inline-block; padding: 2px 8px; margin-right: 6px;
  border: 1px solid var(--border-1); border-radius: 4px;
  background: var(--bg-code); color: var(--el-color-primary);
  font-size: 12px; cursor: pointer; text-decoration: none;
}
.history-link:hover { background: var(--el-color-primary-light-9); }

.snap-cards {
  display: flex; gap: 12px; margin: 12px 0 16px; flex-wrap: wrap;
}
.snap-card {
  background: var(--bg-code);
  border-radius: 6px;
  padding: 8px 16px;
  border: 1px solid var(--border-1);
  min-width: 100px;
}
.snap-card span {
  display: block; font-size: 11px; color: var(--text-3); margin-bottom: 2px;
}
.snap-card b { font-size: 18px; color: var(--text-1); }

.snap-h3 {
  font-size: 13px; font-weight: 600; color: var(--text-3);
  margin: 18px 0 8px; text-transform: uppercase; letter-spacing: 0.5px;
}

.snap-table {
  width: 100%; border-collapse: collapse; background: var(--bg-code);
  border-radius: 6px; overflow: hidden; font-size: 12px;
  border: 1px solid var(--border-1);
}
.snap-table th, .snap-table td {
  padding: 6px 10px; text-align: left; border-bottom: 1px solid var(--border-1);
}
.snap-table th { background: var(--bg-canvas); color: var(--text-3); font-weight: 600; font-size: 11px; }
.snap-table tr:last-child td { border-bottom: 0; }

.snap-errors-block {
  background: rgba(245, 108, 108, 0.08);
  border: 1px solid rgba(245, 108, 108, 0.25);
  border-radius: 6px; padding: 10px 14px; margin: 12px 0;
}
.snap-errors-block b { color: var(--el-color-danger); font-size: 12px; }
.snap-errors-block ul { margin: 6px 0 0 18px; padding: 0; font-size: 12px; }
.snap-errors-block li { margin: 2px 0; }

.snap-pre {
  background: #0f172a; color: #d8e1ee;
  padding: 14px 16px; border-radius: 6px; overflow: auto;
  font-size: 12px; line-height: 1.55; font-family: var(--font-mono);
  max-height: 360px; margin: 6px 0 0;
  white-space: pre-wrap;
}
</style>

<!-- Unscoped fallback.  The scoped .snap-row-actions rules above
     should be enough, but if Vite's hot-reload caches the old CSS
     or a global .el-button rule is winning the cascade, this
     catches it.  Scoped: false, so selectors are global; the
     .health-snap-table prefix keeps them scoped to this page. -->
<style>
.health-snap-table .col-actions .snap-row-actions {
  display: inline-flex !important;
  flex-direction: row !important;
  flex-wrap: nowrap !important;
  align-items: center !important;
  gap: 4px !important;
  vertical-align: middle !important;
  width: auto !important;
}
.health-snap-table .col-actions .snap-row-actions .el-button {
  padding: 4px 8px !important;
  margin: 0 !important;
  display: inline-block !important;
  width: auto !important;
  vertical-align: middle !important;
  white-space: nowrap !important;
}
</style>
