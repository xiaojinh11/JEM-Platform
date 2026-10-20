<script setup lang="ts">
/**
 * 配置中心 / Configuration Archive
 *
 * Layout (per spec, see JEM Platform mock):
 *
 *   ┌──────────────────────────┬─────────────────────────────────────┐
 *   │ 配置清册                  │ 配置详情 · sw-core-01                │
 *   │ ──────                   │  [最新配置] [Diff 对比] [导入]       │
 *   │ ▢ sw-core-01   r3 sys    │                                     │
 *   │   关键字: vrrp vlan acl  │  readonlyPreview ───┐                │
 *   │   来源: backup           │     !                │                │
 *   │ ▢ sw-access-02 r12 bgp  │     sysname sw-core-01│                │
 *   │ ▢ ...                   │     ...              │                │
 *   │                          │     └────────────────┘                │
 *   └──────────────────────────┴─────────────────────────────────────┘
 *
 * Right pane hard rules
 * ──────────────────────
 * 1. The "最新配置" tab is *read-only* at all times.  The only way to
 *    mutate what's shown there is to drive a batch job (left side of
 *    the workflow: pick device → write template → run job → backup
 *    after).  Users CANNOT type into the right pane — it is a
 *    ``<pre>`` block, not an editor.
 *
 * 2. The "Diff 对比" tab is read-only on its inputs.
 *
 * 3. The "导入配置" tab is the only place that takes text input.
 *    Submitting that calls ``api.importConfig``, which records an
 *    audit-logged revision.
 *
 * 4. "回滚" creates a new revision from history — it does not
 *    silently overwrite.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Lock, Search, Promotion } from '@element-plus/icons-vue'
import { api } from '@/api'
import ConfigDiffViewer from '@/components/ConfigDiffViewer.vue'
import { useRouter } from 'vue-router'

// ── types ────────────────────────────────────────────────────────
interface DeviceItem {
  id: number
  name: string
  vendor?: string
  role?: string
  site?: string
}
interface RevisionItem {
  id: number
  revision_no: number
  source: string
  author?: string
  note?: string
  content_hash?: string
  size_bytes: number
  created_at?: string
}

// ── state ────────────────────────────────────────────────────────
const devices = ref<DeviceItem[]>([])
const selectedId = ref<number | null>(null)
const revisions = ref<RevisionItem[]>([])
const fromRev = ref<number | null>(null)
const toRev = ref<number | null>(null)
const fromContent = ref('')
const toContent = ref('')
const latestContent = ref('')
const configText = ref('')
const loading = ref(false)
const tab = ref<'preview' | 'diff' | 'import'>('preview')
const rosterKeyword = ref('')

// ── UI 折叠状态（默认全部折叠，避免版本历史表挡视野） ──
const revisionsCollapsed = ref(true) // 该设备版本历史：默认折叠
const liveCollapsed = ref(true)      // 实时配置工作台：默认折叠
const detailsCollapsed = ref(true)   // 配置详情 tabs：默认折叠（保留 tabs，但收起为 <details>）
// ── 实时配置工作台：占位状态 ──────────────────────
const opLog = ref<string[]>([])
const auditLoading = ref(false)
const auditLog = ref<Array<{ created_at: string; actor: string; action: string; target: string; detail: string }>>([])
// shell 入口（v1: 新标签页打开，不内嵌 iframe 以避免跨页会话日志丢失）
const router = useRouter()
function openShellTab() {
  if (!selectedId.value) return
  const route = router.resolve({ path: '/shell', query: { devices: String(selectedId.value) } })
  window.open(route.href, '_blank', 'noopener')
}
function clearOpLog() {
  opLog.value = []
}
// v1 占位 — 等待后端 audit 接口落地
async function loadAuditLog() {
  auditLoading.value = true
  try {
    // TODO: 接入后端审计接口（GET /api/v1/audit?device_id=<id>）
    // const r = await api.listAudit({ device_id: selectedId.value })
    // auditLog.value = r.items
    auditLog.value = []
  } finally {
    auditLoading.value = false
  }
}

// ── computed ─────────────────────────────────────────────────────
const selectedDevice = computed(
  () => devices.value.find((d) => d.id === selectedId.value) ?? null,
)
const latestRev = computed(() => revisions.value[0] ?? null)
const filteredDevices = computed(() => {
  const kw = rosterKeyword.value.trim().toLowerCase()
  if (!kw) return devices.value
  return devices.value.filter((d) =>
    [d.name, d.vendor, d.role, d.site]
      .filter(Boolean)
      .some((s) => String(s).toLowerCase().includes(kw)),
  )
})
const fromRevNo = computed(() => {
  const r = revisions.value.find((x) => x.id === fromRev.value)
  return r ? `r${r.revision_no}` : '—'
})
const toRevNo = computed(() => {
  const r = revisions.value.find((x) => x.id === toRev.value)
  return r ? `r${r.revision_no}` : '—'
})

// ── helpers ──────────────────────────────────────────────────────
function prettyBytes(b: number): string {
  if (!b) return '0 B'
  const u = ['B', 'KB', 'MB'][Math.min(2, Math.floor(Math.log(b) / Math.log(1024)))]
  return (b / Math.pow(1024, Math.floor(Math.log(b) / Math.log(1024)))).toFixed(1) + ' ' + u
}
function vendorColor(v?: string): '' | 'primary' | 'success' | 'warning' | 'danger' | 'info' {
  if (!v) return ''
  if (v.includes('huawei') || v.includes('vrp')) return 'danger'
  if (v.includes('cisco') || v.includes('ios')) return 'primary'
  if (v.includes('ruijie') || v.includes('rgos')) return 'success'
  if (v.includes('h3c') || v.includes('comware')) return 'warning'
  return 'info'
}
function sourceType(s: string): '' | 'success' | 'warning' | 'info' {
  if (s === 'backup') return 'success'
  if (s === 'rollback') return 'warning'
  if (s === 'import') return 'info'
  return ''
}
function latestRevLabel(d: DeviceItem): string {
  // The "latest revision number" badge on a roster card is only
  // accurate if ``refreshRevisions`` has been called for that device.
  // We show em-dash instead of garbage when data isn't loaded yet.
  return selectedId.value === d.id && revisions.value.length
    ? `r${revisions.value[0].revision_no}`
    : '—'
}

// ── data loaders ─────────────────────────────────────────────────
async function refreshDevices() {
  loading.value = true
  try {
    // The backend list endpoint accepts either a raw list or a
    // paginated {items: …} shape depending on version; handle both.
    const raw = (await api.listDevices({ limit: 1000 })) as unknown
    if (Array.isArray(raw)) {
      devices.value = raw as DeviceItem[]
    } else if (raw && typeof raw === 'object' && Array.isArray((raw as { items?: unknown }).items)) {
      devices.value = (raw as { items: DeviceItem[] }).items
    } else {
      // Fallback: try without limit in case the api surface
      // older versions returned a plain array.
      const alt = await api.listDevices()
      devices.value = (Array.isArray(alt) ? alt : []) as DeviceItem[]
    }
    if (!selectedId.value && devices.value.length) {
      selectedId.value = devices.value[0].id
    }
  } finally {
    loading.value = false
  }
}
async function refreshRevisions() {
  if (!selectedId.value) return
  loading.value = true
  try {
    revisions.value = (await api.listRevisions(selectedId.value)) as RevisionItem[]
    if (revisions.value.length >= 2) {
      fromRev.value = revisions.value[1].id
      toRev.value = revisions.value[0].id
    } else if (revisions.value.length === 1) {
      fromRev.value = null
      toRev.value = revisions.value[0].id
    } else {
      fromRev.value = null
      toRev.value = null
    }
    await loadDiffContent()
    await loadLatestContent()
  } finally {
    loading.value = false
  }
}
async function loadDiffContent() {
  fromContent.value = ''
  toContent.value = ''
  if (fromRev.value) {
    const r = await api.readRevisionContent(fromRev.value)
    fromContent.value = (r as { content: string }).content
  }
  if (toRev.value) {
    const r = await api.readRevisionContent(toRev.value)
    toContent.value = (r as { content: string }).content
  }
}
async function loadLatestContent() {
  latestContent.value = ''
  if (!latestRev.value) return
  const r = await api.readRevisionContent(latestRev.value.id)
  latestContent.value = (r as { content: string }).content
}
async function importConfig() {
  if (!selectedId.value || !configText.value.trim()) {
    ElMessage.warning('请选择设备并填写配置内容')
    return
  }
  await api.importConfig({
    device_id: selectedId.value,
    content: configText.value,
    author: 'ui',
    source: 'import',
    note: 'manual import',
  })
  ElMessage.success('已导入为新版本')
  configText.value = ''
  tab.value = 'preview'
  await refreshRevisions()
}
async function rollbackTo(revId: number) {
  try {
    await ElMessageBox.confirm(
      `确认将设备配置回滚到该历史版本？\n这会创建一条新版本（不会删除历史）。`,
      '回滚确认',
      { type: 'warning', confirmButtonText: '回滚' },
    )
  } catch {
    return
  }
  if (!selectedId.value) return
  const r = await api.readRevisionContent(revId)
  await api.importConfig({
    device_id: selectedId.value,
    content: (r as { content: string }).content,
    author: 'ui',
    source: 'rollback',
    note: `rollback to revision ${revId}`,
  })
  ElMessage.success('已创建回滚版本')
  await refreshRevisions()
}

// ── lifecycle ────────────────────────────────────────────────────
watch(selectedId, () => { if (selectedId.value) refreshRevisions() })
onMounted(async () => {
  await refreshDevices()
})
</script>

<template>
  <div class="page" v-loading="loading">

    <!-- 顶部栏 -->
    <div class="page-header">
      <div class="header-left">
        <span class="page-title">配置中心 · Configuration Archive</span>
        <span class="dim">已索引 {{ devices.length }} 台设备</span>
      </div>
      <div class="header-right">
        <el-button @click="refreshDevices">刷新清单</el-button>
        <el-button @click="refreshRevisions" :disabled="!selectedId">刷新版本</el-button>
      </div>
    </div>

    <!-- 二栏布局 -->
    <div class="archive-grid">

      <!-- ── 左：配置清册 ────────────────────────────────── -->
      <aside class="device-roster">
        <div class="roster-header">
          <span class="roster-title">配置清册</span>
          <el-input
            v-model="rosterKeyword"
            size="small"
            placeholder="搜索设备 / 厂商 / 角色"
            clearable
            :prefix-icon="Search"
          />
        </div>
        <div class="roster-body">
          <div
            v-for="d in filteredDevices"
            :key="d.id"
            class="roster-card"
            :class="{ active: d.id === selectedId }"
            @click="selectedId = d.id"
          >
            <div class="roster-row1">
              <span class="roster-name">{{ d.name }}</span>
              <el-tag size="small" effect="dark" class="roster-rev">
                {{ latestRevLabel(d) }}
              </el-tag>
            </div>
            <div class="roster-row2">
              <el-tag size="small" :type="vendorColor(d.vendor)" effect="plain">
                {{ d.vendor ?? '—' }}
              </el-tag>
              <el-tag size="small" effect="plain">{{ d.role ?? 'core' }}</el-tag>
            </div>
            <div class="roster-row3 dim mono">
              id {{ d.id }} · {{ d.site ?? '—' }}
            </div>
          </div>
          <div v-if="!filteredDevices.length" class="roster-empty">无匹配设备</div>
        </div>
      </aside>

      <!-- ── 右：详情 ─────────────────────────────────────── -->
      <section v-if="selectedDevice" class="detail-area">
        <div class="detail-header">
          <div class="detail-title">
            <span class="device-name">{{ selectedDevice.name }}</span>
            <el-tag size="small" :type="vendorColor(selectedDevice.vendor)" effect="dark">
              {{ selectedDevice.vendor ?? '—' }}
            </el-tag>
            <span v-if="latestRev" class="dim header-meta">
              最新版本 <span class="mono accent">r{{ latestRev.revision_no }}</span>
              · {{ prettyBytes(latestRev.size_bytes) }}
              · {{ latestRev.created_at }}
            </span>
          </div>
        </div>

        <details class="detail-tabs-wrap" :open="!detailsCollapsed">
          <summary class="detail-tabs-summary" @click.prevent="detailsCollapsed = !detailsCollapsed">
            <span>配置详情（最新配置 / Diff 对比 / 导入）</span>
            <span class="dim">{{ detailsCollapsed ? '点击展开 ▾' : '点击收起 ▴' }}</span>
          </summary>
          <div class="detail-tabs">
            <el-tabs v-model="tab">

            <!-- 最新配置：严格只读 -->
            <el-tab-pane label="最新配置" name="preview">
              <div class="readonly-banner">
                <el-icon><Lock /></el-icon>
                <span>
                  此面板严格只读。配置变更只能通过「配置变更 → 模板下发 / 批量任务」流程写入；或通过 [导入配置] 标签页粘贴新配置。
                </span>
              </div>
              <div v-if="!latestRev" class="empty-area">该设备尚无历史版本</div>
              <pre
                v-else
                class="config-readonly"
                :data-revision="`r${latestRev.revision_no}`"
                tabindex="0"
              >{{ latestContent }}</pre>
            </el-tab-pane>

            <!-- Diff 对比 -->
            <el-tab-pane label="Diff 对比" name="diff">
              <div class="diff-bar">
                <div class="diff-controls">
                  <span class="dim mono">FROM</span>
                  <el-select v-model="fromRev" style="width:160px" :teleported="false" @change="loadDiffContent">
                    <el-option
                      v-for="r in revisions"
                      :key="r.id"
                      :value="r.id"
                      :label="`r${r.revision_no} · ${r.source}`"
                    />
                  </el-select>
                  <span class="arrow">→</span>
                  <span class="dim mono">TO</span>
                  <el-select v-model="toRev" style="width:160px" :teleported="false" @change="loadDiffContent">
                    <el-option
                      v-for="r in revisions"
                      :key="r.id"
                      :value="r.id"
                      :label="`r${r.revision_no} · ${r.source}`"
                    />
                  </el-select>
                </div>
              </div>
              <div class="diff-meta mono dim">
                <span>{{ fromRevNo }}</span>
                <span style="margin: 0 6px">→</span>
                <span>{{ toRevNo }}</span>
              </div>
              <ConfigDiffViewer
                :from-content="fromContent"
                :to-content="toContent"
                :from-label="fromRevNo"
                :to-label="toRevNo"
              />
            </el-tab-pane>

            <!-- 导入：唯一可编辑入口 -->
            <el-tab-pane label="导入配置" name="import">
              <el-input
                v-model="configText"
                type="textarea"
                :autosize="{ minRows: 18 }"
                placeholder="粘贴配置文本（VRP / IOS / RGOS）— 提交后将作为一条新版本追加"
                style="font-family: var(--font-mono); font-size: 12.5px"
              />
              <div class="import-actions">
                <el-button @click="configText = ''">清空</el-button>
                <el-button type="primary" :disabled="!configText.trim()" @click="importConfig">
                  导入为新版本
                </el-button>
              </div>
            </el-tab-pane>

          </el-tabs>
          </div>
        </details>

        <!-- 实时配置工作台：左侧 shell 入口 + 右侧双区日志（v1: 占位 + 跳转，shell 实际跑在 /shell 页） -->
        <div class="live-workbench">
          <div class="lw-header">
            <span class="lw-title">实时配置工作台</span>
            <div class="lw-actions">
              <el-button
                size="small"
                :type="liveCollapsed ? 'primary' : 'default'"
                plain
                @click="liveCollapsed = !liveCollapsed"
              >
                {{ liveCollapsed ? '展开工作台' : '收起工作台' }}
              </el-button>
              <el-button
                size="small"
                type="primary"
                :disabled="!selectedId"
                @click="openShellTab"
              >
                <el-icon><Promotion /></el-icon> 在新标签页打开 Shell
              </el-button>
            </div>
          </div>

          <div v-show="!liveCollapsed" class="lw-grid">
            <!-- 左：shell 入口提示 + 跳转；v1 暂不内嵌 iframe 以避免跨页会话日志丢失 -->
            <div class="lw-shell">
              <div class="lw-pane-header">
                <span>设备 Shell</span>
                <span class="dim mono">→ /shell?devices={{ selectedId ?? '—' }}</span>
              </div>
              <div class="lw-placeholder">
                <p>左侧设备 shell 在<strong>「/shell · 多设备终端」</strong>页面运行。</p>
                <p>配置变更 + 实时输出会在那里完成；本页只负责<strong>配置归档 / 版本历史 / 审计记录</strong>。</p>
                <el-button type="primary" plain :disabled="!selectedId" @click="openShellTab">
                  现在打开 Shell
                </el-button>
              </div>
            </div>

            <!-- 右：双区日志 -->
            <div class="lw-logs">
              <!-- 上半：操作日志（命令 + 输出） -->
              <div class="lw-log-pane">
                <div class="lw-pane-header">
                  <span>操作日志</span>
                  <el-button size="small" link @click="clearOpLog">清空</el-button>
                </div>
                <div class="lw-empty">
                  操作日志由 shell 会话写入（v1 占位）。后续接入：让 /shell 页通过 BroadcastChannel / localStorage
                  把 (cmd, output) 推到本页显示。
                </div>
              </div>

              <!-- 下半：审计日志（来自后端 audit 模块） -->
              <div class="lw-log-pane">
                <div class="lw-pane-header">
                  <span>审计日志</span>
                  <el-button size="small" link :loading="auditLoading" @click="loadAuditLog">刷新</el-button>
                </div>
                <div class="lw-empty">
                  审计日志接口 v1 占位（待接入后端 <code>/api/v1/audit?device_id=...</code>）。
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- 设备版本历史（可折叠抽屉） -->
        <div class="revision-list" :class="{ collapsed: revisionsCollapsed }">
          <div class="rl-header">
            <span>该设备版本历史</span>
            <div class="rl-header-right">
              <span class="dim">共 {{ revisions.length }} 条</span>
              <el-button
                size="small"
                text
                @click="revisionsCollapsed = !revisionsCollapsed"
                :title="revisionsCollapsed ? '展开版本历史' : '收起版本历史'"
              >
                {{ revisionsCollapsed ? '展开 ▼' : '收起 ▲' }}
              </el-button>
            </div>
          </div>
          <div v-show="!revisionsCollapsed">
            <el-table :data="revisions" border stripe size="small">
              <el-table-column type="index" label="#" width="50" align="center" />
              <el-table-column label="版本" width="80" align="center">
                <template #default="{ row }">
                  <span class="mono rev-tag">r{{ row.revision_no }}</span>
                </template>
              </el-table-column>
              <el-table-column label="来源" min-width="120">
                <template #default="{ row }">
                  <el-tag :type="sourceType(row.source)" size="small" effect="plain">{{ row.source }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="author" label="作者" min-width="120" />
              <el-table-column prop="note" label="备注" min-width="200" show-overflow-tooltip>
                <template #default="{ row }">
                  <span v-if="row.note">{{ row.note }}</span>
                  <span v-else class="muted">—</span>
                </template>
              </el-table-column>
              <el-table-column label="HASH" min-width="120">
                <template #default="{ row }">
                  <span class="mono hash">{{ (row.content_hash || '').slice(0, 16) }}…</span>
                </template>
              </el-table-column>
              <el-table-column label="大小" width="100" align="right">
                <template #default="{ row }">
                  <span class="mono">{{ prettyBytes(row.size_bytes) }}</span>
                </template>
              </el-table-column>
              <el-table-column prop="created_at" label="创建时间" min-width="170" />
              <el-table-column label="操作" width="120" align="center" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" link type="primary" @click="rollbackTo(row.id)">回滚</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </div>
      </section>

      <section v-else class="detail-area empty-state">
        <div>请从左侧选择设备</div>
      </section>

    </div>
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 14px; }

/* ── 顶部 ───────────────────────────────────────────────── */
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 4px;
}
.header-left { display: flex; align-items: baseline; gap: 12px; }
.page-title {
  font-size: 18px;
  font-weight: 600;
  color: var(--text-1);
}
.dim { color: var(--text-3); font-size: 12px; }

/* ── 二栏布局 ─────────────────────────────────────────── */
.archive-grid {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 16px;
  align-items: stretch;
}

/* ── 左：配置清册 ────────────────────────────────────── */
.device-roster {
  background: var(--bg-canvas);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  display: flex;
  flex-direction: column;
  max-height: calc(100vh - 200px);
  overflow: hidden;
}
.roster-header {
  padding: 10px 12px;
  border-bottom: 1px solid var(--border-1);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.roster-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-1);
  letter-spacing: 0.4px;
}
.roster-body {
  overflow-y: auto;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.roster-card {
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
  cursor: pointer;
  background: var(--bg-input);
  transition: border-color .12s, background .12s;
}
.roster-card:hover { border-color: var(--accent); }
.roster-card.active {
  border-color: var(--accent);
  background: var(--accent-soft);
}
.roster-row1 {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}
.roster-name {
  font-weight: 600;
  font-size: 13px;
  color: var(--text-1);
}
.roster-rev {
  font-family: var(--font-mono);
  font-size: 11px;
}
.roster-row2 {
  display: flex;
  gap: 4px;
  margin-bottom: 4px;
}
.roster-row3 {
  font-size: 11px;
  color: var(--text-3);
}
.roster-empty {
  padding: 16px;
  text-align: center;
  font-size: 12px;
  color: var(--text-3);
}

/* ── 右：详情 ──────────────────────────────────────── */
.detail-area {
  background: var(--bg-canvas);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  max-height: calc(100vh - 200px);
}
.detail-header {
  padding: 10px 16px;
  border-bottom: 1px solid var(--border-1);
}
.detail-title { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.device-name {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-1);
}
.header-meta { font-size: 12px; }

.detail-tabs {
  flex: 1;
  overflow: auto;
  padding: 4px 4px 0;
}

/* ── 只读 banner ─────────────────────────────────────── */
.readonly-banner {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  margin: 8px 12px;
  background: rgba(230, 162, 60, 0.12);
  border: 1px solid rgba(230, 162, 60, 0.35);
  border-radius: var(--radius-sm);
  font-size: 12px;
  color: #c57826;
}
.readonly-banner .el-icon { flex-shrink: 0; }

.config-readonly {
  margin: 8px 12px 12px;
  padding: 14px 16px;
  background: var(--bg-code);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--text-1);
  max-height: calc(100vh - 480px);
  min-height: 280px;
  overflow: auto;
  white-space: pre;
  /* Visible: I am a read-only mirror.  Cursor is "text", not "edit";
     tabIndex lets keyboard users explicitly enter for copy. */
  cursor: text;
  user-select: text;
  -webkit-user-modify: read-only;
}

/* ── Diff / Import ───────────────────────────────────── */
.diff-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 14px;
  background: var(--bg-panel);
  border-bottom: 1px solid var(--border-1);
}
.diff-controls { display: flex; align-items: center; gap: 10px; }
.arrow { color: var(--accent); font-family: var(--font-mono); font-weight: 700; }
.diff-meta {
  padding: 6px 14px 10px;
  font-size: 11px;
  color: var(--text-3);
  letter-spacing: 0.5px;
}
.import-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
}

/* ── 版本列表 ───────────────────────────────────────── */
.revision-list {
  border-top: 1px solid var(--border-1);
  padding: 8px 12px 14px;
  background: var(--bg-input);
  overflow: auto;
  max-height: 36vh;
}
.revision-list.collapsed { max-height: none; padding-bottom: 6px; }
.rl-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  font-size: 12px;
  font-weight: 600;
  padding: 4px 0 8px;
}
.rl-header-right { display: inline-flex; align-items: baseline; gap: 12px; }

/* ── 配置详情 tabs：折叠 ───────────────────────────── */
.detail-tabs-wrap {
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  margin-top: 8px;
  background: var(--bg-canvas);
}
.detail-tabs-summary {
  cursor: pointer;
  user-select: none;
  list-style: none;
  padding: 8px 14px;
  font-size: 12.5px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: space-between;
  outline: none;
}
.detail-tabs-summary::-webkit-details-marker { display: none; }
.detail-tabs-summary:hover { color: var(--accent); }

/* ── 实时配置工作台 ──────────────────────────────── */
.live-workbench {
  margin-top: 12px;
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  background: var(--bg-canvas);
}
.lw-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 14px;
  border-bottom: 1px solid var(--border-1);
}
.lw-title { font-size: 12.5px; font-weight: 600; }
.lw-actions { display: inline-flex; gap: 8px; }
.lw-grid {
  display: grid;
  grid-template-columns: minmax(360px, 1fr) minmax(360px, 1fr);
  gap: 12px;
  padding: 12px 14px 14px;
  min-height: 320px;
}
.lw-shell, .lw-logs {
  display: flex;
  flex-direction: column;
  gap: 8px;
  border: 1px dashed var(--border-1);
  border-radius: var(--radius-md);
  padding: 8px 10px;
  background: var(--bg-input);
  min-height: 280px;
}
.lw-logs { gap: 12px; }
.lw-pane-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-2);
}
.lw-placeholder {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: var(--text-3);
  text-align: center;
  padding: 24px 12px;
}
.lw-placeholder p { margin: 0; line-height: 1.55; }
.lw-placeholder code {
  background: var(--bg-canvas);
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 11.5px;
}
.lw-log-pane {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 6px;
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  padding: 6px 10px;
  background: var(--bg-canvas);
  min-height: 0;
}
.lw-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-3);
  font-size: 12px;
  padding: 12px;
  text-align: center;
}
@media (max-width: 1100px) {
  .lw-grid { grid-template-columns: 1fr; }
}

/* shared atoms */
.rev-tag {
  background: var(--accent-soft);
  color: var(--accent);
  padding: 1px 6px;
  border-radius: 3px;
  font-weight: 600;
}
.hash { color: var(--text-3); font-size: 11.5px; }
.muted { color: var(--text-4); font-size: 12px; }
.mono { font-family: var(--font-mono); }
.accent { color: var(--accent); }

.empty-area {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 240px;
  color: var(--text-3);
  font-size: 13px;
}
.empty-state {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  color: var(--text-3);
}
</style>
