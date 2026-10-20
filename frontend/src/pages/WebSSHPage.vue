<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Terminal } from 'xterm'
import { FitAddon } from 'xterm-addon-fit'
import 'xterm/css/xterm.css'
import { api } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { useWebSSHStore } from '@/stores/webssh'
import { encodeTerminalData, decodeTerminalData, debugTerminalInput } from '@/utils/terminal'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const sshStore = useWebSSHStore()

const deviceId = computed(() => Number(route.params.id))
const presetEndpointId = computed(() => {
  const raw = route.query.endpoint_id
  return raw ? Number(raw) : null
})

const device = ref<Record<string, unknown> | null>(null)
const endpoints = ref<Record<string, unknown>[]>([])
const credentials = ref<Record<string, unknown>[]>([])

// CRT-style connect dialog state
const connectForm = reactive({
  endpointId: null as number | null,
  credentialId: null as number | null,
  cols: 120,
  rows: 30,
  fontSize: 14,
})
const connecting = ref(false)
const connected = ref(false)
const status = ref<'idle' | 'connecting' | 'connected' | 'error' | 'disconnected'>('idle')
const statusText = ref('未连接')
const canEditConnect = ref(true)
const sessionStartedAt = ref<string | null>(null)
const sessionId = ref<string | null>(null)
const isViewer = computed(() => auth.user?.role === 'viewer')
let pingTimer: number | null = null

const container = ref<HTMLDivElement | null>(null)
let term: Terminal | null = null
let fit: FitAddon | null = null
let ws: WebSocket | null = null

// Local echo state: track sent chars to deduplicate device echo
const recentSent: string[] = []
let lastSendTime = 0

const KIND_OPTIONS = [
  { value: 'mgmt_ssh',    label: '管理 SSH（推荐）' },
  { value: 'inband_ssh',  label: '业务 SSH' },
  { value: 'console',     label: 'Console（串口）' },
  { value: 'restconf',    label: 'RESTCONF' },
  { value: 'netconf',     label: 'NETCONF' },
]

const kindLabel = (v: string) => KIND_OPTIONS.find((k) => k.value === v)?.label || v
const credentialLabel = (id: number | null) => {
  if (!id) return '未选择（将使用主用接入的凭据）'
  const c = credentials.value.find((x) => Number(x.id) === id)
  if (!c) return `#${id}`
  return `${c.name}（${c.username} / ${c.secret_kind}）`
}

const refresh = async () => {
  try {
    device.value = await api.getDevice(deviceId.value)
    endpoints.value = await api.listDeviceEndpoints(deviceId.value)
    credentials.value = await api.listCredentials()
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail ?? '加载失败')
  }

  // Pick a default endpoint for the dialog.
  const preset = presetEndpointId.value
  if (preset && endpoints.value.some((e) => Number(e.id) === preset)) {
    connectForm.endpointId = preset
  } else {
    const primary = endpoints.value.find((e) => e.is_primary) ?? endpoints.value[0]
    connectForm.endpointId = primary ? Number(primary.id) : null
  }
  // If picked endpoint has a bound credential, preselect it.
  const ep = endpoints.value.find((e) => Number(e.id) === connectForm.endpointId)
  if (ep && ep.credential_id) {
    connectForm.credentialId = Number(ep.credential_id)
  } else {
    connectForm.credentialId = null
  }
}

const setupTerm = () => {
  if (!container.value) return
  term = new Terminal({
    cursorBlink: true,
    fontFamily: 'Menlo, Consolas, "Courier New", monospace',
    fontSize: connectForm.fontSize,
    cols: connectForm.cols,
    rows: connectForm.rows,
    theme: { background: '#0d1117', foreground: '#e6edf3' },
    convertEol: true,
    scrollback: 5000,
  })
  fit = new FitAddon()
  term.loadAddon(fit)
  term.open(container.value)
  fit.fit()
  term.writeln('\x1b[90m──────────────────────────────────────────────\x1b[0m')
  term.writeln('\x1b[36m  NAW WebSSH\x1b[0m  ·  请在左侧「连接」面板选择连接点')
  term.writeln('\x1b[90m──────────────────────────────────────────────\x1b[0m')
  term.onData((data) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return

    // Debug special keys (Backspace, etc.)
    debugTerminalInput(data, deviceId.value)

    // NO local echo - let device PTY handle all echo (standard SSH behavior)
    // Track sent chars is kept for future fallback logic if needed
    recentSent.push(data)
    if (recentSent.length > 20) recentSent.shift()
    lastSendTime = Date.now()

    try {
      const encoded = encodeTerminalData(data)
      ws.send(JSON.stringify({ type: 'data', data: encoded }))
    } catch (err) {
      console.error(`[Terminal ${deviceId.value}] Send failed:`, err)
      term?.writeln(`\x1b[31m[ERROR]\x1b[0m 发送失败: ${(err as Error).message}`)
    }
  })
  window.addEventListener('resize', resize)
}

const resize = () => {
  if (!term || !fit) return
  try {
    fit.fit()
  } catch {
    // ignore
  }
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
  }
}

const setStatus = (s: typeof status.value, text: string) => {
  status.value = s
  statusText.value = text
}

const cleanupSocket = () => {
  if (ws) {
    // Remove event handlers to prevent stale message processing
    ws.onopen = null
    ws.onmessage = null
    ws.onclose = null
    ws.onerror = null
    try { ws.send(JSON.stringify({ type: 'close' })) } catch { /* ignore */ }
    try { ws.close() } catch { /* ignore */ }
    ws = null
  }
  if (pingTimer !== null) {
    clearInterval(pingTimer)
    pingTimer = null
  }
  // Clear echo state
  recentSent.length = 0
  lastSendTime = 0
}

const connect = async () => {
  if (isViewer.value) {
    ElMessage.warning('只读账户（viewer）无法建立 SSH 会话')
    return
  }
  if (!device.value) {
    ElMessage.warning('设备信息未加载完成')
    return
  }
  if (!connectForm.endpointId) {
    ElMessage.warning('请选择一个连接点（先在「设备详情 → 连接配置」中添加）')
    return
  }
  if (!device.value.is_simulated && !connectForm.credentialId) {
    ElMessage.warning('请选择凭据（先在「设备详情 → 连接配置」中绑定或选择一个凭据）')
    return
  }

  connecting.value = true
  setStatus('connecting', '正在建立连接…')
  try {
    ws = api.openWebSsh(deviceId.value, {
      endpointId: connectForm.endpointId,
      credentialId: connectForm.credentialId,
    })
    ws.binaryType = 'arraybuffer'

    ws.onopen = () => {
      connected.value = true
      connecting.value = false
      setStatus('connected', '已连接')
      canEditConnect.value = false
      term?.writeln('\x1b[32m[NAW]\x1b[0m 已建立连接')
      resize()

      // Start keepalive ping to prevent eNSP 28s idle timeout
      pingTimer = window.setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'ping' }))
        }
      }, 15000)
    }
    ws.onmessage = (event) => {
      if (!connected.value) return  // Ignore stale messages after disconnect
      let msg: { type?: string; data?: string; message?: string; session_id?: string; started_at?: string }
      try {
        msg = JSON.parse(event.data)
      } catch {
        return
      }
      if (msg.type === 'data' && typeof msg.data === 'string') {
        try {
          const decoded = decodeTerminalData(msg.data)
          // Direct display - no deduplication, standard SSH behavior
          term?.write(decoded)
        } catch (err) {
          console.error(`[Terminal ${deviceId.value}] Decode failed:`, err)
        }
      } else if (msg.type === 'info') {
        term?.writeln(`\x1b[36m[NAW]\x1b[0m ${msg.message ?? ''}`)
        if (msg.session_id) sessionId.value = msg.session_id
        if (msg.started_at) sessionStartedAt.value = msg.started_at
      } else if (msg.type === 'error') {
        term?.writeln(`\x1b[31m[NAW]\x1b[0m ${msg.message ?? '连接错误'}`)
        setStatus('error', msg.message ?? '连接错误')
      }
    }
    ws.onclose = (ev) => {
      cleanupSocket()
      connected.value = false
      canEditConnect.value = true
      if (status.value !== 'error') {
        setStatus('disconnected', `已断开（code=${ev.code}）`)
      }
      term?.writeln(`\x1b[33m[NAW]\x1b[0m 连接已断开（${ev.code === 1000 ? '正常关闭' : `code=${ev.code}`}）`)
      term?.writeln('\x1b[90m可以在左侧面板修改连接配置后再次「连接」\x1b[0m')
    }
    ws.onerror = () => {
      // Most errors here are auth-related; the close event will follow.
      setStatus('error', 'WebSocket 错误（可能是身份过期或后端不可用）')
      ElMessage.error('WebSocket 连接失败（可能是身份过期或后端不可用）')
    }
  } catch (e: unknown) {
    connecting.value = false
    const msg = e instanceof Error ? e.message : String(e)
    setStatus('error', msg)
    ElMessage.error(`无法连接：${msg}`)
  }
}

const disconnect = () => {
  cleanupSocket()
  connected.value = false
  canEditConnect.value = true
  setStatus('disconnected', '已断开')
}

const confirmExit = async () => {
  if (connected.value) {
    try {
      await ElMessageBox.confirm('当前 SSH 会话仍在运行，确定要断开并退出吗？', '退出确认', {
        type: 'warning',
        confirmButtonText: '断开并退出',
        cancelButtonText: '取消',
      })
    } catch {
      return
    }
    cleanupSocket()
  }
  router.back()
}

const onEndpointChange = (id: number | null) => {
  // When the user changes the endpoint, update the credential preselection
  // to match (if the endpoint has a bound credential).
  if (!id) {
    connectForm.credentialId = null
    return
  }
  const ep = endpoints.value.find((e) => Number(e.id) === id)
  connectForm.credentialId = ep && ep.credential_id ? Number(ep.credential_id) : null
}

onMounted(async () => {
  setupTerm()
  await refresh()
  await nextTick()
  resize()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  
  // ✅ Key fix: Don't disconnect WebSocket on page unmount
  // Only dispose DOM terminal instance
  term?.dispose()
  term = null
  
  // WebSocket managed by global store remains active
  console.log('[WebSSH] Page unmounted, connection persists')
})
</script>

<template>
  <div class="page webssh-page" v-loading="connecting">
    <el-card class="layout-card">
      <template #header>
        <div class="header">
          <span class="title">
            <el-icon><Connection /></el-icon>
            WebSSH ·
            <span v-if="device">{{ device.name }}</span>
            <span v-else>设备 #{{ deviceId }}</span>
            <el-tag size="small" :type="status === 'connected' ? 'success' : status === 'error' ? 'danger' : 'info'" effect="dark">
              {{ statusText }}
            </el-tag>
          </span>
          <el-space>
            <el-button @click="resize">自适应</el-button>
            <el-button @click="confirmExit">返回</el-button>
          </el-space>
        </div>
      </template>

      <div class="layout">
        <!-- ── 左侧：CRT 风格「连接」面板 ────────────────────── -->
        <aside class="conn-panel">
          <div class="conn-title">连接（CRT 风格）</div>

          <el-form label-position="top" size="small">
            <el-form-item label="设备">
              <el-input :model-value="device?.name ? `${device.name} (#${deviceId})` : `#${deviceId}`" disabled />
            </el-form-item>

            <el-form-item label="协议 / 类型">
              <el-select
                v-model="connectForm.endpointId"
                placeholder="选择连接点"
                :disabled="!canEditConnect"
                @change="onEndpointChange"
                style="width:100%"
              >
                <el-option
                  v-for="ep in endpoints"
                  :key="ep.id"
                  :value="ep.id"
                  :label="`${kindLabel(String(ep.kind))} · ${ep.address}:${ep.port}${ep.is_primary ? ' · 主用' : ''}`"
                />
              </el-select>
            </el-form-item>

            <el-form-item label="凭据">
              <el-select
                v-model="connectForm.credentialId"
                placeholder="选择凭据"
                :disabled="!canEditConnect"
                filterable
                clearable
                style="width:100%"
              >
                <el-option
                  v-for="c in credentials"
                  :key="c.id"
                  :value="c.id"
                  :label="credentialLabel(Number(c.id))"
                />
              </el-select>
              <div class="hint" v-if="!endpoints.length">
                该设备尚未配置任何连接点。请到「设备详情 → 连接配置」中添加。
              </div>
              <div class="hint" v-else-if="!credentials.length">
                系统中还没有凭据。请到「系统设置 → 凭据管理」中创建。
              </div>
            </el-form-item>

            <el-form-item label="终端尺寸">
              <el-space>
                <el-input-number v-model="connectForm.cols" :min="40" :max="400" :disabled="connected" />
                <span>×</span>
                <el-input-number v-model="connectForm.rows" :min="10" :max="120" :disabled="connected" />
              </el-space>
            </el-form-item>

            <el-form-item label="字体大小">
              <el-slider v-model="connectForm.fontSize" :min="10" :max="22" :step="1" :disabled="connected" />
            </el-form-item>

            <div class="conn-actions">
              <el-button
                v-if="!connected"
                type="primary"
                :loading="connecting"
                :disabled="isViewer"
                style="width:100%"
                @click="connect"
              >
                <el-icon><Promotion /></el-icon>
                连接
              </el-button>
              <el-button
                v-else
                type="danger"
                plain
                style="width:100%"
                @click="disconnect"
              >
                <el-icon><SwitchButton /></el-icon>
                断开
              </el-button>
            </div>

            <el-alert
              v-if="isViewer"
              type="warning"
              :closable="false"
              show-icon
              title="只读账户无法建立 SSH 会话"
            />
          </el-form>

          <div class="session-info" v-if="sessionStartedAt">
            <div><strong>会话 ID：</strong>{{ sessionId }}</div>
            <div><strong>开始时间：</strong>{{ new Date(sessionStartedAt).toLocaleString() }}</div>
          </div>
        </aside>

        <!-- ── 右侧：终端 ─────────────────────────────────── -->
        <main class="term-host" ref="container" />
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.webssh-page { height: 100%; }
.layout-card :deep(.el-card__body) { padding: 0; }
.header { display: flex; align-items: center; justify-content: space-between; padding: 12px 18px; border-bottom: 1px solid var(--border-1); }
.title { font-weight: 600; font-size: 16px; display: inline-flex; align-items: center; gap: 8px; }
.layout { display: flex; min-height: calc(100vh - 220px); }
.conn-panel {
  width: 340px;
  padding: 18px;
  border-right: 1px solid var(--border-1);
  background: var(--bg-canvas-soft, #f7f9fc);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.conn-title { font-weight: 600; font-size: 14px; margin-bottom: 12px; display: inline-flex; align-items: center; gap: 6px; }
.conn-actions { margin-top: 4px; margin-bottom: 12px; }
.hint { color: var(--text-3); font-size: 12px; margin-top: 4px; line-height: 1.5; }
.session-info { background: var(--bg-canvas); border: 1px solid var(--border-1); border-radius: 6px; padding: 10px 12px; font-size: 12px; color: var(--text-2); margin-top: 8px; }
.session-info div { margin: 2px 0; }
.term-host { flex: 1; background: #0d1117; min-height: 0; }
</style>
