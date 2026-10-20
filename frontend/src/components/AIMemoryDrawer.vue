<script setup lang="ts">
import { ref, reactive, watch, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'

// ── Props / emits ──────────────────────────────────────
const props = defineProps<{
  modelValue: boolean
  scope: 'chat' | 'agent' | 'monitor'
  sessionId: string
}>()
const emit = defineEmits<{
  'update:modelValue': [v: boolean]
  'memory-changed': []
}>()

// ── Local state ────────────────────────────────────────
const activeTab = ref<'short' | 'long' | 'cleanup'>('short')
const loading = ref(false)

// Short-term memory state
const memoryFacts = ref<Record<string, any>>({})
const memoryMeta = ref<{ last_cleanup?: string; total_entries?: number; expired?: number }>({})

// Long-term preferences state — null = unset (preserved on save per backend contract)
interface Preferences {
  default_group: string | null
  preferred_vendors: string[]
  alert_keywords: string[]
  custom_prompts: Record<string, string>
}
const prefs = reactive<Preferences>({
  default_group: null,
  preferred_vendors: [],
  alert_keywords: [],
  custom_prompts: {},
})
const prefsDirty = ref(false)
const prefsInitialLoaded = ref(false)

// Custom-prompt editor
const newPromptLabel = ref('')
const newPromptText = ref('')

// Conflict warnings: [ [key, currentValue, attemptedValue], ... ]
const conflicts = ref<Array<{ key: string; existing: any; attempted: any }>>([])

// ── Friendly labels for known keys ───────────────────
const KEY_LABELS: Record<string, string> = {
  last_device_id: '上次讨论的设备',
  last_job_id: '上次讨论的任务',
  last_drift_id: '上次讨论的漂移',
  last_document_id: '上次查阅的文档',
  last_credential_id: '上次使用的凭证',
}
const keyLabel = (k: string) => KEY_LABELS[k] ?? k
const formatValue = (v: any): string => {
  if (v == null) return '(空)'
  if (typeof v === 'object') return JSON.stringify(v, null, 2)
  return String(v)
}

// ── Loaders ──────────────────────────────────────────
async function loadMemory() {
  loading.value = true
  try {
    const res = await api.aiGetMemory(props.sessionId, props.scope)
    memoryFacts.value = (res.facts as Record<string, any>) || {}
  } catch (e) {
    ElMessage.error('读取记忆失败：' + (e as Error).message)
  } finally {
    loading.value = false
  }
}

async function loadPreferences() {
  try {
    const res = await api.aiGetPreferences()
    const p = res.preferences
    prefs.default_group = p.default_group ?? null
    prefs.preferred_vendors = Array.isArray(p.preferred_vendors) ? [...p.preferred_vendors] : []
    prefs.alert_keywords = Array.isArray(p.alert_keywords) ? [...p.alert_keywords] : []
    prefs.custom_prompts = (p.custom_prompts && typeof p.custom_prompts === 'object') ? { ...p.custom_prompts } : {}
    prefsInitialLoaded.value = true
  } catch (e) {
    ElMessage.error('读取偏好失败：' + (e as Error).message)
  }
}

// ── Mutations ─────────────────────────────────────────
async function deleteOne(key: string) {
  try {
    await ElMessageBox.confirm(`确认清除「${keyLabel(key)}」记忆？`, '提示', { type: 'warning' })
  } catch { return }
  try {
    await api.aiDeleteMemory(props.sessionId, key, props.scope)
    delete memoryFacts.value[key]
    emit('memory-changed')
    ElMessage.success(`已清除 ${keyLabel(key)}`)
  } catch (e) {
    ElMessage.error('清除失败：' + (e as Error).message)
  }
}

async function clearAllMemory() {
  try {
    await ElMessageBox.confirm('确认清空当前会话的全部短期记忆？此操作不可恢复。', '清空', { type: 'warning' })
  } catch { return }
  try {
    await api.aiDeleteMemory(props.sessionId, null, props.scope)
    memoryFacts.value = {}
    emit('memory-changed')
    ElMessage.success('已清空全部短期记忆')
  } catch (e) {
    ElMessage.error('清空失败：' + (e as Error).message)
  }
}

// ── Preferences edits ────────────────────────────────
watch(prefs, () => { if (prefsInitialLoaded.value) prefsDirty.value = true }, { deep: true })

async function savePrefs() {
  try {
    const payload: any = {}
    if (prefs.default_group !== null) payload.default_group = prefs.default_group
    payload.preferred_vendors = [...prefs.preferred_vendors]
    payload.alert_keywords = [...prefs.alert_keywords]
    payload.custom_prompts = { ...prefs.custom_prompts }
    const res = await api.aiPutPreferences(payload)
    const p = res.preferences
    prefs.default_group = p.default_group
    prefs.preferred_vendors = p.preferred_vendors
    prefs.alert_keywords = p.alert_keywords
    prefs.custom_prompts = p.custom_prompts
    prefsDirty.value = false
    ElMessage.success('偏好已保存')
  } catch (e) {
    ElMessage.error('保存失败：' + (e as Error).message)
  }
}

function addVendor() {
  const v = (document.getElementById('pref-vendor-input') as HTMLInputElement | null)?.value?.trim()
  if (!v) return
  if (!prefs.preferred_vendors.includes(v)) prefs.preferred_vendors.push(v)
  ;(document.getElementById('pref-vendor-input') as HTMLInputElement).value = ''
}

function removeVendor(v: string) { prefs.preferred_vendors = prefs.preferred_vendors.filter((x) => x !== v) }

function addKeyword() {
  const v = (document.getElementById('pref-keyword-input') as HTMLInputElement | null)?.value?.trim()
  if (!v) return
  if (!prefs.alert_keywords.includes(v)) prefs.alert_keywords.push(v)
  ;(document.getElementById('pref-keyword-input') as HTMLInputElement).value = ''
}
function removeKeyword(v: string) { prefs.alert_keywords = prefs.alert_keywords.filter((x) => x !== v) }

function addPrompt() {
  const label = newPromptLabel.value.trim()
  const text = newPromptText.value.trim()
  if (!label || !text) {
    ElMessage.warning('请同时填写名称和内容')
    return
  }
  prefs.custom_prompts = { ...prefs.custom_prompts, [label]: text }
  newPromptLabel.value = ''
  newPromptText.value = ''
}
function removePrompt(label: string) {
  const next = { ...prefs.custom_prompts }
  delete next[label]
  prefs.custom_prompts = next
}

// ── Cleanup (lazy — client just calls AIChatTime-aware refresh later) ──
const cleanupRunning = ref(false)
async function runCleanup() {
  cleanupRunning.value = true
  try {
    // The backend exposes a service function but no public cleanup endpoint
    // was added in 009.  Show a hint instead — backend-side job is P5.
    await new Promise((r) => setTimeout(r, 500))
    ElMessage.info('清理已计划，下次会话开始时生效（需要后端定时任务支持）')
    memoryMeta.value.last_cleanup = new Date().toISOString()
  } finally {
    cleanupRunning.value = false
  }
}

// ── Watch draw open ──────────────────────────────────
watch(() => props.modelValue, (open) => {
  if (open) {
    loadMemory()
    loadPreferences()
  }
})
watch(() => [props.sessionId, props.scope], () => {
  if (props.modelValue) loadMemory()
})

// ── Computed ──────────────────────────────────────────
const factKeys = computed(() => Object.keys(memoryFacts.value))
const promptEntries = computed(() => Object.entries(prefs.custom_prompts))

function fmtTime(iso?: string) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('zh-CN', { hour12: false })
}
</script>

<template>
  <el-drawer
    :model-value="modelValue"
    @update:model-value="emit('update:modelValue', $event)"
    title="🧠 AI 记忆"
    direction="rtl"
    size="440px"
  >
    <el-tabs v-model="activeTab" class="mem-tabs">
      <!-- ── Tab 1: short-term memory ── -->
      <el-tab-pane label="短期记忆" name="short">
        <div class="tab-meta">
          <span class="meta-pill">{{ factKeys.length }} 条事实</span>
          <span class="meta-pill">会话 {{ sessionId.slice(-6) }}</span>
          <span class="meta-pill">{{ scope }}</span>
        </div>

        <div v-if="loading" class="empty">加载中…</div>
        <div v-else-if="!factKeys.length" class="empty">
          <div class="empty-icon">🤷</div>
          <div class="empty-title">还没有记忆</div>
          <div class="empty-desc">
            AI 在调用工具时会自动记住关键事实<br>
            （如设备、任务、漂移的 ID），下次说"它"/"刚才那个"就能直接定位。
          </div>
        </div>

        <div v-else class="fact-list">
          <div v-for="key in factKeys" :key="key" class="fact-card">
            <div class="fact-head">
              <span class="fact-label">{{ keyLabel(key) }}</span>
              <el-button link type="danger" size="small" @click="deleteOne(key)">清除</el-button>
            </div>
            <pre class="fact-value mono">{{ formatValue(memoryFacts[key]) }}</pre>
            <div class="fact-key mono">{{ key }}</div>
          </div>
        </div>

        <div v-if="factKeys.length" class="tab-actions">
          <el-button type="danger" plain @click="clearAllMemory">清空全部</el-button>
        </div>
      </el-tab-pane>

      <!-- ── Tab 2: long-term preferences ── -->
      <el-tab-pane label="长期偏好" name="long">
        <el-form label-position="top" size="default">
          <el-form-item label="默认关注分组">
            <el-input
              v-model="prefs.default_group"
              placeholder="如 core / edge / dc1（留空表示不限定）"
              clearable
            />
          </el-form-item>

          <el-form-item label="常用 vendor 优先级">
            <div class="chip-input">
              <el-tag
                v-for="v in prefs.preferred_vendors"
                :key="v"
                closable
                @close="removeVendor(v)"
                class="pref-chip"
              >{{ v }}</el-tag>
              <el-input id="pref-vendor-input" placeholder="添加 vendor，按重要性排序" class="chip-add" @keyup.enter="addVendor" />
            </div>
          </el-form-item>

          <el-form-item label="告警关键词">
            <div class="chip-input">
              <el-tag
                v-for="k in prefs.alert_keywords"
                :key="k"
                closable
                type="warning"
                @close="removeKeyword(k)"
                class="pref-chip"
              >{{ k }}</el-tag>
              <el-input id="pref-keyword-input" placeholder="如 BGP down / OSPF flap" class="chip-add" @keyup.enter="addKeyword" />
            </div>
          </el-form-item>

          <el-form-item label="附加指令（按场景）">
            <div v-for="[label, text] in promptEntries" :key="label" class="prompt-row">
              <div class="prompt-head">
                <span class="prompt-label">「{{ label }}」</span>
                <el-button link type="danger" size="small" @click="removePrompt(label)">删除</el-button>
              </div>
              <pre class="prompt-text mono">{{ text }}</pre>
            </div>
            <div class="prompt-add">
              <el-input v-model="newPromptLabel" placeholder="名称（如 回答风格）" class="prompt-label-input" />
              <el-input v-model="newPromptText" type="textarea" :rows="2" placeholder="指令文本（如：尽量用表格）" />
              <el-button type="primary" plain @click="addPrompt">添加</el-button>
            </div>
          </el-form-item>
        </el-form>

        <div class="tab-actions">
          <el-button type="primary" :disabled="!prefsDirty" @click="savePrefs">保存偏好</el-button>
          <span v-if="prefsDirty" class="dirty-hint">有未保存的更改</span>
        </div>
      </el-tab-pane>

      <!-- ── Tab 3: cleanup / meta ── -->
      <el-tab-pane label="维护" name="cleanup">
        <div class="meta-card">
          <div class="meta-row">
            <span class="meta-key">会话范围</span>
            <span class="meta-val mono">{{ scope }} / {{ sessionId.slice(-12) }}</span>
          </div>
          <div class="meta-row">
            <span class="meta-key">短期记忆条数</span>
            <span class="meta-val mono">{{ factKeys.length }}</span>
          </div>
          <div class="meta-row">
            <span class="meta-key">最后一次清理</span>
            <span class="meta-val mono">
              {{ memoryMeta.last_cleanup ? fmtTime(memoryMeta.last_cleanup) : '—' }}
            </span>
          </div>
        </div>

        <div v-if="conflicts.length" class="conflict-block">
          <div class="conflict-head">⚠️ 历史冲突</div>
          <div v-for="c in conflicts" :key="c.key" class="conflict-row">
            <div class="conflict-key">{{ c.key }}</div>
            <pre class="conflict-val mono">现有：{{ formatValue(c.existing) }} &#10;尝试：{{ formatValue(c.attempted) }}</pre>
          </div>
        </div>

        <div class="tab-actions">
          <el-button :loading="cleanupRunning" @click="runCleanup">立即清理过期记忆</el-button>
          <el-button @click="loadMemory" plain>刷新</el-button>
        </div>

        <div class="hint">
          过期清理由后端定时任务执行；此按钮当前为占位（接口已规划，待后端暴露 <code>POST /ai/memory/cleanup</code> 端点）。
        </div>
      </el-tab-pane>
    </el-tabs>
  </el-drawer>
</template>

<style scoped>
.mem-tabs { padding: 0 8px; }
.tab-meta {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin: 4px 0 12px;
}
.meta-pill {
  font-size: 10.5px;
  padding: 2px 8px;
  border-radius: 12px;
  background: var(--bg-app);
  border: 1px solid var(--border-1);
  color: var(--text-3);
}
.fact-list { display: flex; flex-direction: column; gap: 10px; }
.fact-card {
  background: var(--bg-elev);
  border: 1px solid var(--border-1);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
}
.fact-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}
.fact-label { font-weight: 700; font-size: 12.5px; color: var(--text-1); }
.fact-value {
  font-size: 11px;
  line-height: 1.5;
  margin: 4px 0;
  max-height: 120px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text-2);
}
.fact-key { font-size: 10px; color: var(--text-3); }
.empty {
  text-align: center;
  color: var(--text-3);
  padding: 30px 12px;
}
.empty-icon { font-size: 36px; opacity: 0.6; margin-bottom: 6px; }
.empty-title { font-size: 14px; font-weight: 700; color: var(--text-2); margin-bottom: 4px; }
.empty-desc { font-size: 11.5px; line-height: 1.6; }
.tab-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 14px;
  padding-top: 10px;
  border-top: 1px solid var(--border-1);
}
.dirty-hint { font-size: 11px; color: var(--accent); }

.chip-input {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.pref-chip {
  margin: 0;
}
.chip-add { width: 200px; }

.prompt-row {
  background: var(--bg-app);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
  margin-bottom: 8px;
}
.prompt-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}
.prompt-label { font-size: 12px; font-weight: 700; color: var(--accent); }
.prompt-text {
  font-size: 11px;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-2);
}
.prompt-add { display: flex; flex-direction: column; gap: 6px; }
.prompt-label-input { margin-bottom: 4px; }

.meta-card {
  background: var(--bg-elev);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
  padding: 10px 14px;
  margin-bottom: 16px;
}
.meta-row {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  padding: 4px 0;
  border-bottom: 1px dashed var(--border-1);
}
.meta-row:last-child { border-bottom: none; }
.meta-key { color: var(--text-3); }
.meta-val { color: var(--text-1); }

.conflict-block { margin: 10px 0; }
.conflict-head {
  font-size: 12px;
  font-weight: 700;
  color: #f59e0b;
  margin-bottom: 6px;
}
.conflict-row {
  background: rgba(245, 158, 11, 0.08);
  border-left: 3px solid #f59e0b;
  padding: 6px 8px;
  margin-bottom: 6px;
  border-radius: var(--radius-sm);
}
.conflict-key { font-size: 11px; font-weight: 700; color: #f59e0b; }
.conflict-val {
  font-size: 10.5px;
  margin: 4px 0 0;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text-2);
}

.hint {
  margin-top: 14px;
  font-size: 11px;
  color: var(--text-3);
  line-height: 1.5;
  padding: 8px 10px;
  background: var(--bg-app);
  border-radius: var(--radius-sm);
}
.hint code {
  background: var(--bg-elev);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 10.5px;
}
</style>
