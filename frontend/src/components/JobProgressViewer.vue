<script setup lang="ts">
/**
 * JobProgressViewer — live progress for a single batch job.
 *
 * Subscribes to /api/v1/ws/jobs/{jobId} and renders per-device,
 * per-step status with the live command output streaming in.
 *
 * Layout:
 *   ┌───────────────────────────────────────────────┐
 *   │  Top status bar (job status, % done, ETA)     │
 *   ├───────────────────────────────────────────────┤
 *   │  Device cards:                                │
 *   │   ▢ SW01  ✓precheck  ✓backup  ▷apply  ◌after │
 *   │     [terminal-style log window]               │
 *   └───────────────────────────────────────────────┘
 *
 * Why we don't reuse xterm.js here: the device output we show is
 * *batched*, not interactive.  A scrollable pre-formatted text area
 * is honest about that — operators can copy / scroll / search without
 * being confused into thinking they can type into it.
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Connection, Loading, WarningFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api'

export interface ProgressDevice {
  device_id: number
  device_name: string
  steps: Record<string, {
    status: 'pending' | 'running' | 'succeeded' | 'failed' | 'needs_verification' | 'skipped'
    log?: string
    error?: string
  }>
  /** Streamed device output (concatenated as events arrive). */
  log: string
  /** Whether the WS has emitted any update for this device yet. */
  seen: boolean
}

const props = defineProps<{
  jobId: number
  /** Initial device list — fetched by the parent so we don't re-fetch. */
  devices: Array<{ id: number; name: string }>
  /** Auto-scroll the log window as events arrive. */
  autoScroll?: boolean
  /** Show the connection-status indicator. */
  showStatus?: boolean
}>()

const emit = defineEmits<{
  (e: 'status', status: 'connecting' | 'connected' | 'disconnected' | 'reconnecting' | 'error' | 'closed', message?: string): void
}>()

// ── state ───────────────────────────────────────────────────────────
const state = ref<Map<number, ProgressDevice>>(new Map())
const overallStatus = ref<string>('unknown')
const summary = ref<Record<string, number>>({})
const reconnectAttempts = ref(0)
const MAX_RECONNECT = 10

let ws: WebSocket | null = null
let heartbeatTimer: number | null = null
let reconnectTimer: number | null = null
let closedByUser = false

// Initialize from props so we render an empty scaffold before any event
// arrives — operators see "device 1 waiting…" immediately, not blank.
function bootstrap() {
  const map = new Map<number, ProgressDevice>()
  for (const d of props.devices) {
    map.set(d.id, {
      device_id: d.id,
      device_name: d.name,
      steps: {
        PRECHECK: { status: 'pending' },
        BACKUP_BEFORE: { status: 'pending' },
        EXECUTE: { status: 'pending' },
        BACKUP_AFTER: { status: 'pending' },
      },
      log: '',
      seen: false,
    })
  }
  state.value = map
}

bootstrap()
watch(() => props.devices, () => bootstrap(), { deep: true })

// ── WebSocket lifecycle ─────────────────────────────────────────────
function connect() {
  if (ws && ws.readyState === WebSocket.OPEN) return
  closedByUser = false
  emit('status', reconnectAttempts.value > 0 ? 'reconnecting' : 'connecting')

  try {
    const token = localStorage.getItem('naw.token')
    if (!token) throw new Error('not authenticated')
    const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const proto = `naw-token,${encodeURIComponent(token)}`
    ws = new WebSocket(`${scheme}//${location.host}/api/v1/ws/jobs/${props.jobId}`, [proto])
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : String(e)
    emit('status', 'error', msg)
    scheduleReconnect()
    return
  }

  ws.onopen = () => {
    reconnectAttempts.value = 0
    emit('status', 'connected')
    // Heartbeat: ping every 25s so the server's 30s wait_for doesn't
    // trip idle-timeout.  The server doesn't actually require a pong,
    // it just needs *some* traffic to know we're alive.
    heartbeatTimer = window.setInterval(() => {
      try { ws?.send('ping') } catch { /* ignore */ }
    }, 25_000)
  }

  ws.onmessage = (ev) => {
    let msg: Record<string, unknown>
    try { msg = JSON.parse(ev.data) } catch { return }
    handle(msg)
  }

  ws.onerror = () => {
    emit('status', 'error', 'WebSocket 错误')
  }

  ws.onclose = () => {
    if (heartbeatTimer) {
      clearInterval(heartbeatTimer)
      heartbeatTimer = null
    }
    if (closedByUser) {
      emit('status', 'closed')
      return
    }
    emit('status', 'disconnected')
    scheduleReconnect()
  }
}

function scheduleReconnect() {
  if (reconnectTimer) return
  if (reconnectAttempts.value >= MAX_RECONNECT) {
    emit('status', 'error', `已重试 ${MAX_RECONNECT} 次，放弃`)
    return
  }
  const delay = Math.min(1000 * (2 ** reconnectAttempts.value), 15_000)
  reconnectAttempts.value += 1
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null
    connect()
  }, delay)
}

function disconnect() {
  closedByUser = true
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer)
    heartbeatTimer = null
  }
  if (ws) {
    try { ws.close() } catch { /* ignore */ }
    ws = null
  }
}

// ── event handling ─────────────────────────────────────────────────
function handle(msg: Record<string, unknown>) {
  const t = msg.type
  if (t === 'heartbeat') return
  if (t === 'backlog_marker') return

  if (t === 'status_update') {
    overallStatus.value = String(msg.status ?? overallStatus.value)
    if (msg.summary && typeof msg.summary === 'object') {
      summary.value = msg.summary as Record<string, number>
    }
    if (overallStatus.value === 'succeeded' || overallStatus.value === 'failed'
        || overallStatus.value === 'partial' || overallStatus.value === 'canceled') {
      emit('status', 'closed')
      disconnect()
    }
    return
  }

  if (t === 'step_update') {
    const deviceId = Number(msg.device_id)
    const stepKind = String(msg.step_kind)
    const status = String(msg.status)
    const entry = state.value.get(deviceId)
    if (!entry) return
    entry.steps[stepKind] = {
      status: status as ProgressDevice['steps'][string]['status'],
      log: (msg.log as string | undefined) ?? entry.steps[stepKind]?.log,
      error: (msg.error as string | undefined) ?? entry.steps[stepKind]?.error,
    }
    entry.seen = true
    state.value = new Map(state.value)
    return
  }

  if (t === 'log') {
    const deviceId = Number(msg.device_id)
    const text = String(msg.log ?? '')
    const entry = state.value.get(deviceId)
    if (!entry) return
    // Append, but cap the per-device log so a long-running job doesn't
    // grow this ref into the megabytes.
    const next = (entry.log ? entry.log + '\n' : '') + text
    entry.log = next.slice(-8000)
    entry.seen = true
    state.value = new Map(state.value)
  }
}

// ── computed ────────────────────────────────────────────────────────
const deviceList = computed(() => Array.from(state.value.values()))

function stepBadgeType(s: string): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
  switch (s) {
    case 'succeeded': return 'success'
    case 'running': return 'warning'
    case 'failed': return 'danger'
    case 'needs_verification': return 'warning'
    case 'skipped': return 'info'
    default: return 'info'
  }
}
function stepLabel(s: string): string {
  switch (s) {
    case 'pending': return '等待'
    case 'running': return '执行中'
    case 'succeeded': return '成功'
    case 'failed': return '失败'
    case 'needs_verification': return '待复核'
    case 'skipped': return '跳过'
    default: return s
  }
}
function overallBadgeType(s: string): 'success' | 'warning' | 'danger' | 'info' {
  switch (s) {
    case 'succeeded': return 'success'
    case 'running': return 'warning'
    case 'failed': return 'danger'
    case 'partial': return 'warning'
    case 'canceled': return 'info'
    default: return 'info'
  }
}

const completedCount = computed(() => {
  let n = 0
  for (const d of deviceList.value) {
    if (d.steps.EXECUTE && d.steps.EXECUTE.status !== 'pending' && d.steps.EXECUTE.status !== 'running') n++
  }
  return n
})

onMounted(() => {
  connect()
})
onBeforeUnmount(() => {
  disconnect()
})

defineExpose({ connect, disconnect })
</script>

<template>
  <div class="job-progress-viewer">
    <div class="status-bar">
      <el-tag :type="overallBadgeType(overallStatus)" effect="dark" size="default">
        <el-icon v-if="overallStatus === 'running'"><Loading /></el-icon>
        <span style="margin-left: 4px">任务状态：{{ overallStatus }}</span>
      </el-tag>
      <el-tag effect="plain">
        进度：{{ completedCount }} / {{ deviceList.length }}
      </el-tag>
      <template v-if="summary && Object.keys(summary).length">
        <el-tag v-if="summary.succeeded" type="success" effect="plain" size="small">
          ✓ {{ summary.succeeded }}
        </el-tag>
        <el-tag v-if="summary.failed" type="danger" effect="plain" size="small">
          ✗ {{ summary.failed }}
        </el-tag>
        <el-tag v-if="summary.needs_verification" type="warning" effect="plain" size="small">
          ? {{ summary.needs_verification }}
        </el-tag>
        <el-tag v-if="summary.skipped" type="info" effect="plain" size="small">
          ↷ {{ summary.skipped }}
        </el-tag>
      </template>
    </div>

    <div class="device-grid">
      <div v-for="d in deviceList" :key="d.device_id" class="device-card">
        <div class="device-card-header">
          <span class="device-name">
            <el-icon><Connection /></el-icon>
            {{ d.device_name }}
            <span class="device-id">#{{ d.device_id }}</span>
          </span>
          <el-tag
            v-if="d.steps.EXECUTE"
            :type="stepBadgeType(d.steps.EXECUTE.status)"
            effect="dark"
            size="small"
          >
            {{ stepLabel(d.steps.EXECUTE.status) }}
          </el-tag>
        </div>

        <div class="step-pipeline">
          <div
            v-for="(label, key) in {
              PRECHECK: '预检',
              BACKUP_BEFORE: '备份前',
              EXECUTE: '下发',
              BACKUP_AFTER: '备份后',
            }"
            :key="String(key)"
            class="step"
            :class="`step-${d.steps[String(key)]?.status ?? 'pending'}`"
          >
            <span class="step-icon">
              <template v-if="d.steps[String(key)]?.status === 'succeeded'">✓</template>
              <template v-else-if="d.steps[String(key)]?.status === 'failed'">✗</template>
              <template v-else-if="d.steps[String(key)]?.status === 'running'"><Loading /></template>
              <template v-else>○</template>
            </span>
            <span class="step-label">{{ label }}</span>
          </div>
        </div>

        <div v-if="d.steps.EXECUTE?.error" class="device-error">
          <el-icon><WarningFilled /></el-icon>
          {{ d.steps.EXECUTE.error }}
        </div>

        <div class="device-log" :class="{ 'is-empty': !d.log }">
          <pre v-if="d.log">{{ d.log }}</pre>
          <span v-else class="empty-hint">
            {{ d.seen ? '设备已连接，但暂无 EXECUTE 输出' : '等待设备开始执行…' }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.job-progress-viewer {
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: var(--bg-canvas);
  border-radius: var(--radius-md);
}
.status-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: var(--bg-input);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
  flex-wrap: wrap;
}
.device-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 12px;
}
.device-card {
  background: var(--bg-input);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.device-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px dashed var(--border-1);
  padding-bottom: 8px;
}
.device-name {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  color: var(--text-1);
  font-size: 13px;
}
.device-id {
  font-family: Menlo, Consolas, monospace;
  font-size: 11px;
  color: var(--text-3);
  font-weight: normal;
}
.step-pipeline {
  display: flex;
  gap: 6px;
  align-items: stretch;
}
.step {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 6px 4px;
  border-radius: 4px;
  font-size: 11px;
  gap: 2px;
  border: 1px solid var(--border-1);
  background: var(--bg-canvas);
  color: var(--text-3);
}
.step-icon {
  font-size: 14px;
  line-height: 1;
}
.step-running {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-soft);
}
.step-succeeded {
  border-color: #67c23a;
  color: #67c23a;
  background: rgba(103, 194, 58, 0.1);
}
.step-failed {
  border-color: #f56c6c;
  color: #f56c6c;
  background: rgba(245, 108, 108, 0.1);
}
.step-needs_verification {
  border-color: #e6a23c;
  color: #e6a23c;
  background: rgba(230, 162, 60, 0.1);
}
.step-skipped {
  border-style: dashed;
  color: var(--text-3);
}
.device-error {
  background: rgba(245, 108, 108, 0.1);
  border: 1px solid rgba(245, 108, 108, 0.3);
  padding: 6px 10px;
  border-radius: 4px;
  font-size: 12px;
  color: #f56c6c;
  font-family: Menlo, Consolas, monospace;
  white-space: pre-wrap;
  word-break: break-all;
  display: flex;
  align-items: flex-start;
  gap: 6px;
}
.device-log {
  background: #0b1220;
  color: #d1e3f8;
  border-radius: 4px;
  padding: 8px 10px;
  font-family: Menlo, Consolas, monospace;
  font-size: 11.5px;
  line-height: 1.5;
  max-height: 220px;
  overflow-y: auto;
  border: 1px solid var(--border-2);
}
.device-log pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
}
.device-log.is-empty {
  background: var(--bg-canvas);
  color: var(--text-3);
}
.empty-hint {
  display: block;
  text-align: center;
  font-style: italic;
}
</style>
