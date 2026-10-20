<script setup lang="ts">
import { onMounted, ref, reactive, nextTick, watch, computed } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import type { AIChatMessage, AIStats, AIAgentLog } from '@/api'
import AIConfigDrawer from '@/components/AIConfigDrawer.vue'
import AIMemoryDrawer from '@/components/AIMemoryDrawer.vue'
import PromptBar from '@/components/PromptBar.vue'
import type { PromptAttachment } from '@/components/PromptBar.vue'

type Scope = 'chat' | 'agent' | 'monitor'
const activeScope = ref<Scope>('chat')

// session id (per-scope so each tab has its own conversation)
const sessionId = ref(`sess-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`)
const sessions = reactive<Record<Scope, AIChatMessage[]>>({ chat: [], agent: [], monitor: [] })

const sending = ref(false)
const stats = ref<AIStats | null>(null)
const configOpen = ref(false)
const memoryOpen = ref(false)
const drawer = ref<HTMLElement | null>(null)
const recentTools = ref<AIAgentLog[]>([])
const todayTurn = ref(0)
// File picker — still needed for @attach from PromptBar
const fileInput = ref<HTMLInputElement | null>(null)
// ── Attachments for this turn ────────────────────────────────────────
//  type values: 'image' (base64 PNG/JPG), 'file' (text content), 'url'
interface PendingAttachment {
  type: 'image' | 'file' | 'url'
  name: string
  url?: string  // data: URI for images
  text?: string  // inline text for files
  value?: string // raw URL string
  size?: number
}
const pendingAttachments = ref<PendingAttachment[]>([])

// Allowed attachment size (kept in bytes so we can show friendly text)
const MAX_FILE_BYTES = 5 * 1024 * 1024  // 5 MB text / single image
const MAX_IMAGE_BYTES = 10 * 1024 * 1024 // 10 MB for inline images

const personaOptions = [
  { value: 'chat',    label: 'AI Chat',    desc: '问答诊断 · 被动响应', icon: '💬' },
  { value: 'agent',   label: 'AI Agent',   label2: 'AI 操控', desc: '主动调用工具 · 操作 NAW', icon: '🤖' },
  { value: 'monitor', label: 'AI Monitor', desc: '全局监控 · 主动告警', icon: '📡' },
] as const

const messages = computed(() => sessions[activeScope.value])

// ── React PromptBar config ─────────────────────────────────────────────
const promptBarSources = [
  { key: 'file', name: '上传文件', description: '图片、文本、配置等', attach: true },
  { key: 'url', name: '网页抓取', description: '输入 URL 后由 AI 读取' },
  { key: 'device', name: '设备查询', description: '查询设备列表和状态' },
  { key: 'drift', name: '漂移事件', description: '查看当前漂移情况' },
]
const promptBarCommands = [
  { key: 'scan', name: '/scan', description: '扫描全网配置漂移' },
  { key: 'summary', name: '/summary', description: '全网设备状态总览' },
  { key: 'backup', name: '/backup', description: '批量备份设备配置' },
  { key: 'compare', name: '/compare', description: '两台设备配置对比' },
]
const promptBarModels = computed(() => stats.value?.model ? [{ key: stats.value.model, name: stats.value.model, tag: '当前配置' }] : [])
const promptBarEfforts = ['低', '中', '高', '极高', '最大']

async function onPromptBarSend(payload: { text: string; attachments: PromptAttachment[]; model?: { key: string }; effort?: string }) {
  const text = payload.text.trim()
  if (!text && !payload.attachments.length) return
  if (!stats.value?.enabled) {
    ElMessage.warning('AI Agent 未启用，请先在右上角配置并启用 API Key')
    configOpen.value = true
    return
  }
  sending.value = true
  const attachmentsForTurn = [...payload.attachments]
  pendingAttachments.value = []
  sessions[activeScope.value].push({ role: 'user', content: text })
  scrollToBottom()
  const placeholder: AIChatMessage = { role: 'assistant', content: '' }
  sessions[activeScope.value].push(placeholder)
  scrollToBottom()
  try {
    const res = await api.aiChat(activeScope.value, sessionId.value, text, attachmentsForTurn)
    if (res.error) {
      placeholder.content = '❌ ' + res.error
      ElMessage.error(res.error)
    } else {
      placeholder.content = res.reply || '(空回复)'
      placeholder.tokens_used = res.tokens
      if (res.tools_used?.length) {
        placeholder.content += '\n\n```\n🔧 ' +
          res.tools_used.map((t) => `${t.ok ? '✓' : '✗'} ${t.tool} (${t.ms}ms)`).join(' · ') + '\n```'
      }
      // ── AI Plan proposal ────────────────────────────────────────────
      // If the LLM invoked propose_config_plan this round, the resolved
      // plan rides on the response. Pin it on the placeholder so the
      // "Dry-Run AI plan" button can find it without losing it to a
      // re-render.
      if (res.plan && Array.isArray(res.plan.commands) && res.plan.commands.length > 0) {
        placeholder.ai_plan = res.plan
      }
    }
    todayTurn.value++
    await Promise.all([refreshStats(), refreshAgentLogs()])
  } catch (e) {
    placeholder.content = '❌ ' + (e as Error).message
  } finally {
    sending.value = false
    scrollToBottom()
  }
}

// ── AI Plan → Job pipeline bridge ─────────────────────────────────────
// The LLM's ``propose_config_plan`` tool returns a typed payload;
// we attach it to the assistant message and let the user click a
// button to push it to the JobsPage Dry-Run form. The plan rides in
// the router query so JobsPage can hydrate ``form`` from it.
const router = useRouter()
const openAiPlanDryRun = (plan: {
  commands: string[]
  vendor: string
  notes: string[]
  summary: string
}) => {
  if (!plan?.commands?.length) return
  // We base64-encode the plan payload because router query strings are
  // URL-length-limited and command arrays can easily blow past 2 KB.
  // JobsPage's hydration code looks for the ``ai_plan_b64`` key.
  try {
    const json = JSON.stringify(plan)
    const b64 = btoa(unescape(encodeURIComponent(json)))
    router.push({ path: '/jobs', query: { ai_plan_b64: b64 } })
  } catch (err) {
    ElMessage.error('无法跳转到批量任务页：' + (err as Error).message)
  }
}

function scrollToBottom() {
  nextTick(() => {
    if (drawer.value) drawer.value.scrollTop = drawer.value.scrollHeight
  })
}

const loadHistory = async () => {
  try {
    const list = await api.aiHistory(activeScope.value, sessionId.value)
    sessions[activeScope.value] = list || []
    scrollToBottom()
  } catch { /* fresh session */ }
}

const refreshStats = async () => {
  stats.value = await api.aiStats()
}

const refreshAgentLogs = async () => {
  recentTools.value = await api.aiAgentLogs(20)
}

const switchScope = async (scope: Scope) => {
  activeScope.value = scope
  await loadHistory()
}

const newSession = () => {
  if (messages.value.length > 0) {
    ElMessageBox.confirm('确认开启新会话？当前会话历史会保留。', '新会话', { type: 'info' })
      .then(async () => {
        sessionId.value = `sess-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`
        sessions[activeScope.value] = []
      })
      .catch(() => {})
  } else {
    sessionId.value = `sess-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`
  }
}

const clearSession = async () => {
  await ElMessageBox.confirm('确认清空当前会话？', '清空', { type: 'warning' })
  await api.aiClearHistory(activeScope.value, sessionId.value)
  sessions[activeScope.value] = []
}

// ── Attachment handlers ──────────────────────────────────────────────
function pickFiles() {
  fileInput.value?.click()
}

async function onFilePicked(ev: Event) {
  const t = ev.target as HTMLInputElement
  if (!t.files || !t.files.length) return
  for (const file of Array.from(t.files)) {
    await ingestFile(file)
  }
  // Reset the input so re-selecting the same file fires the change event.
  t.value = ''
}

async function ingestFile(file: File) {
  if (file.size > MAX_FILE_BYTES && !file.type.startsWith('image/')) {
    ElMessage.warning(`${file.name} 超过 ${MAX_FILE_BYTES / 1024 / 1024} MB 文本上限`)
    return
  }
  if (file.type.startsWith('image/')) {
    if (file.size > MAX_IMAGE_BYTES) {
      ElMessage.warning(`${file.name} 超过 ${MAX_IMAGE_BYTES / 1024 / 1024} MB 图片上限`)
      return
    }
    const dataUrl = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(reader.result as string)
      reader.onerror = () => reject(new Error('文件读取失败'))
      reader.readAsDataURL(file)
    })
    pendingAttachments.value.push({ type: 'image', name: file.name, url: dataUrl, size: file.size })
  } else {
    // Treat as text content — best effort UTF-8 decode; truncate if huge
    const text = await file.text()
    pendingAttachments.value.push({
      type: 'file',
      name: file.name,
      text: text.length > 50000 ? text.slice(0, 50000) + '\n…(已截断)' : text,
      size: file.size,
    })
  }
}

function removePendingAttachment(index: number) {
  pendingAttachments.value.splice(index, 1)
}

function clearPendingAttachments() {
  pendingAttachments.value = []
}

function fmtTime(iso?: string) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleTimeString('zh-CN', { hour12: false })
}

watch(activeScope, () => scrollToBottom())

onMounted(async () => {
  await Promise.all([loadHistory(), refreshStats(), refreshAgentLogs()])
})
</script>

<template>
  <div class="page">
    <!-- Header -->
    <div class="page-header">
      <div>
        <span class="page-title">AI Agent</span>
        <span class="page-subtitle">OpenAI 兼容 · Chat 问答 / Agent 操控 / Monitor 监控</span>
      </div>
      <div class="header-actions">
        <el-button @click="newSession">新会话</el-button>
        <el-button @click="clearSession" type="danger" plain>清空</el-button>
        <el-button @click="memoryOpen = true" type="warning" plain>🧠 记忆</el-button>
        <el-button type="primary" plain @click="configOpen = true">⚙️ 配置</el-button>
      </div>
    </div>

    <!-- Stats strip -->
    <div v-if="stats" class="ai-stats">
      <div class="st-card">
        <div class="st-label">对话总数</div>
        <div class="st-value mono">{{ stats.total_chats }}</div>
      </div>
      <div class="st-card">
        <div class="st-label">今日对话</div>
        <div class="st-value mono">{{ todayTurn }}</div>
      </div>
      <div class="st-card">
        <div class="st-label">工具调用</div>
        <div class="st-value mono">{{ stats.tool_calls_total }}</div>
      </div>
      <div class="st-card">
        <div class="st-label">成功率</div>
        <div class="st-value mono">{{ Math.round(stats.tool_success_rate * 100) }}%</div>
      </div>
      <div class="st-card">
        <div class="st-label">Tokens</div>
        <div class="st-value mono">{{ stats.total_tokens }}</div>
      </div>
      <div class="st-card st-status">
        <div class="st-label">状态</div>
        <div class="st-value st-state" :class="stats.enabled ? 'ok' : 'off'">
          {{ stats.enabled ? '已启用' : '未启用' }}
        </div>
      </div>
    </div>

    <!-- Persona tabs -->
    <div class="persona-tabs">
      <div
        v-for="p in personaOptions"
        :key="p.value"
        class="persona-tab"
        :class="{ active: activeScope === p.value }"
        @click="switchScope(p.value)"
      >
        <span class="persona-icon">{{ p.icon }}</span>
        <div class="persona-info">
          <div class="persona-title">{{ p.label }}</div>
          <div class="persona-desc">{{ p.desc }}</div>
        </div>
      </div>
    </div>

    <!-- Chat area + side panel -->
    <div class="ai-body">
      <div class="chat-pane">
        <div ref="drawer" class="chat-scroll">
          <div v-if="!messages.length" class="chat-empty">
            <div class="empty-icon">{{ personaOptions.find(p => p.value === activeScope)?.icon }}</div>
            <div class="empty-title">开始对话</div>
            <div class="empty-desc">
              当前 Persona: <b>{{ personaOptions.find(p => p.value === activeScope)?.label }}</b><br>
              <span v-if="activeScope === 'chat'">纯对话模式，可询问配置、网络故障、运维问题</span>
              <span v-else-if="activeScope === 'agent'">Agent 模式，可调用工具读取设备配置、查看漂移事件、触发扫描</span>
              <span v-else>Monitor 模式，长期值守，可调度工具扫描设备并生成报告</span>
            </div>
            <div v-if="activeScope !== 'chat'" class="empty-tips">
              <div class="tip-row">试试：<span class="tip-cmd">"扫描全网配置漂移"</span></div>
              <div class="tip-row">试试：<span class="tip-cmd">"列出所有思科设备"</span></div>
              <div class="tip-row">试试：<span class="tip-cmd">"最近的严重漂移有哪些？"</span></div>
            </div>
          </div>
          <div
            v-for="(m, idx) in messages"
            :key="idx"
            class="msg-row"
            :class="`role-${m.role}`"
          >
            <div class="msg-bubble">
              <div class="msg-head">
                <span class="msg-role">{{ m.role }}</span>
                <span v-if="m.tokens_used" class="msg-tokens mono">{{ m.tokens_used }} tok</span>
              </div>
              <pre class="msg-content"><code>{{ m.content }}</code></pre>
              <!-- AI plan proposal card. Only assistant turns carry
                   ``ai_plan``; user/tool turns never do. We render the
                   commands, the vendor hint, and a button to push the
                   plan into the JobsPage Dry-Run form. -->
              <div
                v-if="m.role === 'assistant' && m.ai_plan && m.ai_plan.commands?.length"
                class="ai-plan-card"
              >
                <div class="ai-plan-head">
                  <span class="ai-plan-tag">📋 AI 配置计划</span>
                  <span class="ai-plan-summary">{{ m.ai_plan.summary }}</span>
                </div>
                <ul class="ai-plan-cmds">
                  <li v-for="(cmd, ci) in m.ai_plan.commands" :key="ci">
                    <code>{{ cmd }}</code>
                  </li>
                </ul>
                <div v-if="m.ai_plan.notes?.length" class="ai-plan-notes">
                  <div v-for="(note, ni) in m.ai_plan.notes" :key="ni">{{ note }}</div>
                </div>
                <div class="ai-plan-foot">
                  <span class="ai-plan-meta">
                    vendor: <code>{{ m.ai_plan.vendor || '(auto)' }}</code>
                    · {{ m.ai_plan.commands.length }} 条命令
                  </span>
                  <el-button
                    type="primary"
                    size="small"
                    @click="openAiPlanDryRun(m.ai_plan)"
                  >
                    Dry-Run 该 AI 计划 →
                  </el-button>
                </div>
              </div>
            </div>
          </div>
        </div>
        <input
          ref="fileInput"
          class="file-input-hidden"
          type="file"
          multiple
          accept=".txt,.md,.json,.yaml,.yml,.cfg,.conf,.log,.csv,image/*"
          @change="onFilePicked"
        >
        <PromptBar
          placeholder="输入问题（Enter 发送，Shift+Enter 换行）"
          :sources="promptBarSources"
          :commands="promptBarCommands"
          :attachments="pendingAttachments"
          :models="promptBarModels"
          :efforts="promptBarEfforts"
          :busy="sending"
          @send="onPromptBarSend"
          @stop="sending = false"
          @attach="pickFiles"
          @remove-attachment="removePendingAttachment"
          @clear-attachments="clearPendingAttachments"
          class="chat-prompt-bar"
        />
      </div>

      <!-- Right panel: recent tool calls (only show in agent/monitor modes) -->
      <aside v-if="activeScope !== 'chat'" class="tool-panel">
        <div class="tp-head">
          <span class="tp-title">最近工具调用</span>
        </div>
        <div class="tp-list">
          <div v-for="t in recentTools" :key="t.id" class="tp-row" :class="{ ok: t.ok, fail: !t.ok }">
            <div class="tp-row-head">
              <span class="tp-tool">{{ t.tool }}</span>
              <span class="tp-time mono">{{ fmtTime(t.created_at) }}</span>
            </div>
            <div class="tp-row-meta">
              <span class="tp-user">{{ t.username }}</span>
              <span class="tp-ms mono">{{ t.duration_ms }}ms</span>
            </div>
            <pre v-if="t.result_preview" class="tp-preview"><code>{{ t.result_preview.slice(0, 200) }}{{ t.result_preview.length > 200 ? '…' : '' }}</code></pre>
          </div>
          <div v-if="!recentTools.length" class="tp-empty">暂无工具调用记录</div>
        </div>
      </aside>
    </div>

    <AIConfigDrawer v-model="configOpen" @saved="refreshStats" />
    <AIMemoryDrawer
      v-model="memoryOpen"
      :scope="activeScope"
      :session-id="sessionId"
      @memory-changed="loadHistory"
    />
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  margin-bottom: 14px;
}
.page-title {
  font-size: 17px;
  font-weight: 700;
  color: var(--text-1);
  margin-right: 10px;
}
.page-subtitle {
  font-size: 11px;
  color: var(--text-3);
  letter-spacing: 0.5px;
  font-family: var(--font-mono);
}
.header-actions {
  display: flex;
  gap: 8px;
}

/* Stats strip */
.ai-stats {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 10px;
  margin-bottom: 14px;
}
.st-card {
  background: var(--bg-elev);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  padding: 12px;
  text-align: center;
}
.st-label {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 1px;
  color: var(--text-3);
  text-transform: uppercase;
  margin-bottom: 4px;
}
.st-value {
  font-size: 22px;
  font-weight: 700;
  color: var(--text-1);
}
.st-state.ok { color: #22c55e; }
.st-state.off { color: #ef4444; }

/* Persona tabs */
.persona-tabs {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  margin-bottom: 14px;
}
.persona-tab {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 14px;
  background: var(--bg-elev);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all 0.15s ease;
}
.persona-tab:hover {
  border-color: var(--accent);
  transform: translateY(-1px);
}
.persona-tab.active {
  border-color: var(--accent);
  background: var(--accent-soft);
  box-shadow: 0 2px 8px rgba(34, 211, 238, 0.15);
}
.persona-icon { font-size: 24px; }
.persona-info { flex: 1; min-width: 0; }
.persona-title { font-size: 13px; font-weight: 700; color: var(--text-1); }
.persona-desc { font-size: 11px; color: var(--text-3); margin-top: 2px; }

/* Chat body */
.ai-body {
  display: flex;
  gap: 10px;
  height: calc(100vh - 320px);
  min-height: 400px;
}
.chat-pane {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: var(--bg-elev);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  overflow: hidden;
}
.chat-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
}
.chat-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  text-align: center;
  color: var(--text-3);
}
.empty-icon { font-size: 48px; opacity: 0.5; margin-bottom: 12px; }
.empty-title { font-size: 18px; font-weight: 700; color: var(--text-1); margin-bottom: 6px; }
.empty-desc { font-size: 13px; line-height: 1.6; }
.empty-tips { margin-top: 18px; }
.tip-row { font-size: 12px; margin: 4px 0; }
.tip-cmd {
  background: var(--bg-app);
  border: 1px solid var(--border-1);
  border-radius: 4px;
  padding: 2px 8px;
  color: var(--accent);
  font-family: var(--font-mono);
  font-size: 11.5px;
}

.msg-row {
  display: flex;
  margin-bottom: 12px;
}
.role-user { justify-content: flex-end; }
.role-assistant { justify-content: flex-start; }
.role-tool { justify-content: flex-start; opacity: 0.6; }
.msg-bubble {
  max-width: 80%;
  padding: 10px 14px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-1);
}
.role-user .msg-bubble {
  background: var(--accent-soft);
  border-color: rgba(34, 211, 238, 0.3);
}
.role-assistant .msg-bubble {
  background: var(--bg-app);
}
.role-tool .msg-bubble {
  background: rgba(167, 139, 250, 0.08);
  border-color: rgba(167, 139, 250, 0.25);
}
.msg-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
  font-size: 10px;
  color: var(--text-3);
  letter-spacing: 0.5px;
}
.msg-role { text-transform: uppercase; font-weight: 700; }
.msg-tokens {
  background: var(--bg-elev);
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 9.5px;
}
.msg-content {
  font-family: var(--font-mono);
  font-size: 12.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
  color: var(--text-1);
}

/* AI plan proposal card (assistant turns only). */
.ai-plan-card {
  margin-top: 8px;
  border: 1px solid var(--accent-2);
  border-radius: var(--radius-sm);
  background: rgba(120, 180, 255, 0.06);
  padding: 10px 12px;
  font-family: var(--font-sans);
}
.ai-plan-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.ai-plan-tag {
  font-size: 11px;
  font-weight: 600;
  color: var(--accent-1);
  letter-spacing: 0.04em;
}
.ai-plan-summary {
  color: var(--text-1);
  font-size: 13px;
}
.ai-plan-cmds {
  margin: 6px 0;
  padding: 0;
  list-style: none;
  max-height: 220px;
  overflow-y: auto;
  background: rgba(0, 0, 0, 0.18);
  border-radius: 6px;
  padding: 8px 10px;
}
.ai-plan-cmds li code {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-1);
}
.ai-plan-notes {
  font-size: 12px;
  color: var(--text-2);
  margin: 6px 0;
  line-height: 1.5;
}
.ai-plan-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 6px;
  flex-wrap: wrap;
}
.ai-plan-meta {
  font-size: 11.5px;
  color: var(--text-2);
}
.ai-plan-meta code {
  font-family: var(--font-mono);
}

.file-input-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  clip-path: inset(50%);
  white-space: nowrap;
}

.chat-input {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border-top: 1px solid var(--border-1);
  background: var(--bg-app);
}
.chat-input .input-row {
  display: flex;
  gap: 10px;
  align-items: flex-end;
}
.chat-input textarea {
  flex: 1;
  background: var(--bg-canvas);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
  padding: 8px 12px;
  font-family: var(--font-mono);
  font-size: 13px;
  color: var(--text-1);
  resize: none;
}
.chat-input textarea:focus {
  outline: none;
  border-color: var(--accent);
}
.chat-input .attach-toolbar {
  display: flex;
  gap: 6px;
  align-items: center;
  flex-wrap: wrap;
}
.chat-input .attach-hint {
  font-size: 11px;
  color: var(--text-4);
  margin-left: auto;
  font-family: var(--font-mono);
}
.chat-input .url-row {
  display: flex;
  gap: 6px;
  align-items: center;
}
.pending-attach {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 4px 0;
}
.pending-chip {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 6px 4px 4px;
  background: var(--bg-canvas);
  border: 1px solid var(--border-1);
  border-radius: 8px;
  max-width: 240px;
}
.pending-chip.is-image {
  background: var(--accent-soft);
}
.chip-thumb {
  width: 32px;
  height: 32px;
  object-fit: cover;
  border-radius: 4px;
}
.chip-icon {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  background: var(--bg-panel);
  border-radius: 4px;
}
.chip-meta {
  flex: 1;
  min-width: 0;
}
.chip-name {
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.chip-sub {
  font-size: 10px;
  color: var(--text-4);
}
.chip-remove {
  background: transparent;
  border: none;
  cursor: pointer;
  color: var(--text-3);
  font-size: 14px;
  padding: 0 4px;
}
.chip-remove:hover { color: var(--accent); }

/* Tool panel */
.tool-panel {
  width: 320px;
  background: var(--bg-elev);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.tp-head {
  padding: 10px 14px;
  border-bottom: 1px solid var(--border-1);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 1px;
  color: var(--text-3);
  text-transform: uppercase;
}
.tp-list {
  flex: 1;
  overflow-y: auto;
  padding: 10px;
}
.tp-row {
  background: var(--bg-app);
  border: 1px solid var(--border-1);
  border-left: 3px solid var(--text-3);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
  margin-bottom: 8px;
}
.tp-row.ok { border-left-color: #22c55e; }
.tp-row.fail { border-left-color: #ef4444; }
.tp-row-head {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--text-1);
  font-weight: 600;
}
.tp-time { font-size: 10px; color: var(--text-3); font-weight: 400; }
.tp-row-meta {
  display: flex;
  gap: 10px;
  font-size: 10px;
  color: var(--text-3);
  margin: 3px 0;
}
.tp-preview {
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-2);
  background: var(--bg-canvas);
  padding: 6px;
  border-radius: 4px;
  margin: 6px 0 0;
  max-height: 100px;
  overflow: hidden;
  white-space: pre-wrap;
  word-break: break-all;
}
.tp-empty {
  color: var(--text-3);
  text-align: center;
  font-size: 12px;
  padding: 20px 0;
}
</style>
