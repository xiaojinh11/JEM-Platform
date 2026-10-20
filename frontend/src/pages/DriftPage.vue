<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, reactive, computed } from 'vue'
import { Download } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import type { DriftSummary, DriftListItem, DriftDetail } from '@/api'

const loading = ref(false)
const summary = ref<DriftSummary | null>(null)
const list = ref<DriftListItem[]>([])
const detail = ref<DriftDetail | null>(null)
const detailDrawer = ref(false)
const scanning = ref(false)
let pollTimer: number | null = null

const filters = reactive({ severity: '', status: '', q: '' })

const filteredList = computed(() => {
  let items = list.value
  if (filters.severity) items = items.filter((i) => i.severity === filters.severity)
  if (filters.status) items = items.filter((i) => i.status === filters.status)
  if (filters.q.trim()) {
    const q = filters.q.toLowerCase()
    items = items.filter(
      (i) =>
        i.device_name.toLowerCase().includes(q) ||
        i.vendor.toLowerCase().includes(q) ||
        (i.change_hint || '').toLowerCase().includes(q),
    )
  }
  return items
})

const refresh = async () => {
  try {
    const [s, l] = await Promise.all([api.driftSummary(), api.driftList({ limit: 200 })])
    summary.value = s
    list.value = l
  } catch {
  } finally {
    loading.value = false
  }
}

const openDetail = async (item: DriftListItem) => {
  detail.value = await api.driftDetail(item.id)
  detailDrawer.value = true
}

const acknowledge = async (item: DriftListItem) => {
  await ElMessageBox.confirm(`确认将「${item.device_name}」的漂移事件标记为已处理？`, '确认处理', { type: 'info' })
  await api.driftAcknowledge(item.id)
  ElMessage.success('已标记为已处理')
  await refresh()
}

async function exportData() {
  try {
    await api.exportData('drift', 'json')
    ElMessage.success('漂移数据已导出')
  } catch (err: unknown) {
    const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail ?? '导出失败')
  }
}

const triggerScan = async () => {
  scanning.value = true
  try {
    const res = await api.driftTriggerScan()
    ElMessage.success(`扫描完成：共 ${res.scanned} 台，发现 ${res.drifted} 条漂移`)
    await refresh()
  } catch (e) {
    ElMessage.error('扫描失败：' + (e as Error).message)
  } finally {
    scanning.value = false
  }
}

const severityTagType = (s: string): '' | 'danger' | 'warning' | 'info' => {
  if (s === 'critical') return 'danger'
  if (s === 'warning') return 'warning'
  return 'info'
}
const severityLabel = (s: string) =>
  s === 'critical' ? '严重' : s === 'warning' ? '警告' : '提示'
const statusLabel = (s: string) => (s === 'new' ? '新' : '已确认')

function fmtTime(iso?: string | null) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

// ── 可视化数据 ──────────────────────────────────────────────
// 24 小时漂移趋势 (按小时聚合)
const hourlyTrend = computed(() => {
  const buckets: Record<string, { hour: string; critical: number; warning: number; info: number }> = {}
  const now = new Date()
  for (let i = 23; i >= 0; i--) {
    const d = new Date(now.getTime() - i * 3600_000)
    const k = `${String(d.getHours()).padStart(2, '0')}:00`
    buckets[k] = { hour: k, critical: 0, warning: 0, info: 0 }
  }
  list.value.forEach((it) => {
    const t = new Date(it.detected_at)
    if (Number.isNaN(t.getTime())) return
    const diffH = (now.getTime() - t.getTime()) / 3600_000
    if (diffH > 24 || diffH < 0) return
    const k = `${String(t.getHours()).padStart(2, '0')}:00`
    if (buckets[k]) {
      buckets[k][it.severity as 'critical' | 'warning' | 'info']++
    }
  })
  return Object.values(buckets)
})

// 24h 趋势 SVG 折线
const trendPath = computed(() => {
  const data = hourlyTrend.value
  const w = 100, h = 40
  const max = Math.max(1, ...data.map((d) => d.critical + d.warning + d.info))
  return data
    .map((d, i) => {
      const x = (i / Math.max(1, data.length - 1)) * w
      const y = h - ((d.critical + d.warning + d.info) / max) * h
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
})
const trendFillPath = computed(() => {
  const p = trendPath.value
  if (!p) return ''
  return `${p} L100,40 L0,40 Z`
})
const trendMax = computed(() => Math.max(1, ...hourlyTrend.value.map((d) => d.critical + d.warning + d.info)))

// 饼图：变更类型分布
const hintDistribution = computed(() => {
  const map: Record<string, number> = {}
  list.value.forEach((it) => {
    const k = it.change_hint || '未分类'
    map[k] = (map[k] || 0) + 1
  })
  const total = Object.values(map).reduce((a, b) => a + b, 0)
  return Object.entries(map)
    .sort((a, b) => b[1] - a[1])
    .map(([label, count]) => ({
      label,
      count,
      pct: total > 0 ? Math.round((count / total) * 100) : 0,
    }))
})
const pieColors = ['#22d3ee', '#f59e0b', '#ef4444', '#a78bfa', '#34d399', '#fb7185', '#60a5fa', '#facc15']
const pieSlices = computed(() => {
  let cumPct = 0
  return hintDistribution.value.map((d, i) => {
    const startPct = cumPct
    cumPct += d.pct
    return {
      ...d,
      color: pieColors[i % pieColors.length],
      startPct,
      endPct: cumPct,
      dashArray: `${d.pct} ${100 - d.pct}`,
      dashOffset: -startPct,
    }
  })
})

// Top 设备（按漂移次数）
const topDevices = computed(() => {
  const map: Record<string, { name: string; vendor: string; count: number; severity: string }> = {}
  list.value.forEach((it) => {
    const key = `${it.device_name}|${it.vendor}`
    const ex = map[key]
    if (ex) {
      ex.count++
      if (it.severity === 'critical') ex.severity = 'critical'
    } else {
      map[key] = { name: it.device_name, vendor: it.vendor, count: 1, severity: it.severity }
    }
  })
  return Object.values(map).sort((a, b) => b.count - a.count).slice(0, 6)
})

onMounted(async () => {
  loading.value = true
  await refresh()
  pollTimer = window.setInterval(refresh, 5000)
})

onBeforeUnmount(() => {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
})
</script>

<template>
  <div class="page" v-loading="loading">
    <!-- Header -->
    <div class="page-header">
      <div>
        <span class="page-title">配置漂移检测</span>
        <span class="page-subtitle">每 10 秒自动扫描 · 实时基准对比</span>
      </div>
      <div class="header-actions">
        <el-button :icon="Download" @click="exportData">导出</el-button>
        <el-button type="primary" :loading="scanning" @click="triggerScan">
          触发全网扫描
        </el-button>
      </div>
    </div>

    <!-- ── Row 1: 5 stat cards ─────────────────────────────────── -->
    <div v-if="summary" class="drift-summary">
      <div class="ds-card ds-total">
        <div class="ds-label">TOTAL</div>
        <div class="ds-value mono">{{ summary.total }}</div>
        <div class="ds-sub">全部漂移记录</div>
      </div>
      <div class="ds-card ds-new">
        <div class="ds-label">PENDING</div>
        <div class="ds-value mono" :class="{ 'num-danger': summary.new_count > 0 }">
          {{ summary.new_count }}
        </div>
        <div class="ds-sub">待处理事件</div>
      </div>
      <div class="ds-card ds-critical" :class="{ pulse: (summary.by_severity.critical ?? 0) > 0 }">
        <div class="ds-label">CRITICAL</div>
        <div class="ds-value mono num-danger">{{ summary.by_severity.critical ?? 0 }}</div>
        <div class="ds-sub">≥20 行变更</div>
      </div>
      <div class="ds-card ds-warning">
        <div class="ds-label">WARNING</div>
        <div class="ds-value mono num-warn">{{ summary.by_severity.warning ?? 0 }}</div>
        <div class="ds-sub">5-19 行变更</div>
      </div>
      <div class="ds-card ds-info">
        <div class="ds-label">INFO</div>
        <div class="ds-value mono">{{ summary.by_severity.info ?? 0 }}</div>
        <div class="ds-sub">&lt;5 行变更</div>
      </div>
    </div>

    <!-- ── Row 2: visualisations ──────────────────────────────── -->
    <div class="drift-vis">
      <!-- 24h trend -->
      <div class="vis-card">
        <div class="vis-head">
          <span class="vis-title">24 小时漂移趋势</span>
          <span class="vis-meta">峰值: {{ trendMax }}</span>
        </div>
        <svg viewBox="0 0 100 40" class="trend-svg" preserveAspectRatio="none">
          <defs>
            <linearGradient id="trendGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="#ef4444" stop-opacity="0.3" />
              <stop offset="100%" stop-color="#ef4444" stop-opacity="0" />
            </linearGradient>
          </defs>
          <path :d="trendFillPath" fill="url(#trendGrad)" />
          <path :d="trendPath" fill="none" stroke="#ef4444" stroke-width="0.6" />
        </svg>
        <div class="trend-axis">
          <span v-for="h in [0, 6, 12, 18, 23]" :key="h" class="mono">
            {{ String(hourlyTrend[h]?.hour ?? '').slice(0, 5) }}
          </span>
        </div>
      </div>

      <!-- Pie: 变更类型 -->
      <div class="vis-card">
        <div class="vis-head">
          <span class="vis-title">变更类型分布</span>
        </div>
        <div class="pie-row">
          <svg viewBox="0 0 36 36" class="pie-svg">
            <circle cx="18" cy="18" r="15.915" fill="var(--bg-app)" />
            <circle
              v-for="(s, i) in pieSlices"
              :key="i"
              cx="18"
              cy="18"
              r="15.915"
              fill="transparent"
              :stroke="s.color"
              stroke-width="6"
              :stroke-dasharray="s.dashArray + ' 100'"
              :stroke-dashoffset="s.dashOffset"
              transform="rotate(-90 18 18)"
            />
          </svg>
          <div class="pie-legend">
            <div v-for="(s, i) in pieSlices" :key="i" class="legend-row">
              <span class="legend-dot" :style="{ background: s.color }"></span>
              <span class="legend-label">{{ s.label }}</span>
              <span class="legend-pct mono">{{ s.pct }}%</span>
            </div>
            <div v-if="!pieSlices.length" class="legend-empty">暂无数据</div>
          </div>
        </div>
      </div>

      <!-- Top 设备 -->
      <div class="vis-card">
        <div class="vis-head">
          <span class="vis-title">高漂移设备 TOP 6</span>
        </div>
        <div class="top-devs">
          <div v-for="d in topDevices" :key="d.name" class="top-row">
            <span class="top-rank mono">#</span>
            <div class="top-info">
              <div class="top-name">{{ d.name }}</div>
              <div class="top-vendor">{{ d.vendor }}</div>
            </div>
            <span class="top-count mono">{{ d.count }}</span>
            <span class="top-sev" :class="`sev-${d.severity}`">
              {{ severityLabel(d.severity) }}
            </span>
          </div>
          <div v-if="!topDevices.length" class="legend-empty">暂无漂移事件</div>
        </div>
      </div>
    </div>

    <!-- ── Filters & table ─────────────────────────────────── -->
    <div class="drift-filters">
      <el-input
        v-model="filters.q"
        placeholder="搜索设备 / 厂商 / 变更类型"
        clearable
        style="width: 260px"
      >
        <template #prefix><span>🔍</span></template>
      </el-input>
      <el-select v-model="filters.severity" placeholder="严重程度" clearable style="width: 130px">
        <el-option label="严重" value="critical" />
        <el-option label="警告" value="warning" />
        <el-option label="提示" value="info" />
      </el-select>
      <el-select v-model="filters.status" placeholder="处理状态" clearable style="width: 130px">
        <el-option label="新" value="new" />
        <el-option label="已确认" value="acknowledged" />
      </el-select>
      <span class="filter-count">{{ filteredList.length }} / {{ list.length }} 条记录</span>
    </div>

    <div class="drift-table-wrap">
      <el-table :data="filteredList" stripe style="width: 100%">
        <el-table-column label="严重" width="90">
          <template #default="{ row }">
            <el-tag :type="severityTagType(row.severity)" size="small" effect="dark">
              {{ severityLabel(row.severity) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <span :class="row.status === 'new' ? 'status-new' : 'status-ack'">
              {{ statusLabel(row.status) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="设备" min-width="160">
          <template #default="{ row }">
            <span class="mono">{{ row.device_name }}</span>
            <span class="vendor-tag">{{ row.vendor }}</span>
          </template>
        </el-table-column>
        <el-table-column label="变更摘要" min-width="200">
          <template #default="{ row }">
            <span class="change-hint">{{ row.change_hint || '未分类' }}</span>
            <span class="diff-stat">
              <span class="diff-add">+{{ row.lines_added }}</span>
              <span class="diff-del">-{{ row.lines_removed }}</span>
            </span>
          </template>
        </el-table-column>
        <el-table-column label="发现时间" width="170">
          <template #default="{ row }">
            <span class="mono dim">{{ fmtTime(row.detected_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="处理人" width="100">
          <template #default="{ row }">
            <span class="dim">{{ row.acknowledged_by || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="openDetail(row)">详情</el-button>
            <el-button
              v-if="row.status === 'new'"
              size="small"
              type="success"
              @click="acknowledge(row)"
            >
              确认
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- Detail drawer -->
    <el-drawer v-model="detailDrawer" direction="rtl" size="600px" :with-header="false">
      <div v-if="detail" class="dd-root">
        <div class="dd-head">
          <div class="dd-head-title">
            <span class="dd-icon">⚠️</span>
            <div>
              <div class="dd-device">{{ detail.device_name }}</div>
              <div class="dd-vendor mono">{{ detail.vendor }}</div>
            </div>
          </div>
          <el-tag :type="severityTagType(detail.severity)" effect="dark">
            {{ severityLabel(detail.severity) }}
          </el-tag>
        </div>
        <div class="dd-meta">
          <div class="dd-meta-row">
            <span class="dd-meta-label">发现时间</span>
            <span class="mono">{{ fmtTime(detail.detected_at) }}</span>
          </div>
          <div class="dd-meta-row">
            <span class="dd-meta-label">变更类型</span>
            <span>{{ detail.change_hint || '未分类' }}</span>
          </div>
          <div class="dd-meta-row">
            <span class="dd-meta-label">行变更</span>
            <span>
              <span class="diff-add">+{{ detail.lines_added }}</span>
              <span style="margin: 0 4px">/</span>
              <span class="diff-del">-{{ detail.lines_removed }}</span>
            </span>
          </div>
          <div v-if="detail.acknowledged_by" class="dd-meta-row">
            <span class="dd-meta-label">已确认</span>
            <span>{{ detail.acknowledged_by }} {{ fmtTime(detail.acknowledged_at) }}</span>
          </div>
        </div>
        <div class="dd-section-title">DIFF 摘要</div>
        <pre class="dd-diff"><code>{{ detail.diff_snippet || '(无)' }}</code></pre>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
/* ── Header ──────────────────────────────────────────────────── */
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

/* ── Stat cards ──────────────────────────────────────────────── */
.drift-summary {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 10px;
  margin-bottom: 14px;
}
.ds-card {
  background: var(--bg-elev);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  padding: 14px 16px;
  text-align: center;
  transition: box-shadow 0.15s;
}
.ds-card:hover { box-shadow: 0 2px 8px rgba(0,0,0,0.15); }
.ds-label {
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 1px;
  color: var(--text-3);
  text-transform: uppercase;
  margin-bottom: 6px;
}
.ds-value {
  font-size: 28px;
  font-weight: 800;
  color: var(--text-1);
  line-height: 1;
  margin-bottom: 4px;
}
.ds-sub { font-size: 10px; color: var(--text-3); letter-spacing: 0.5px; }
.ds-total { border-color: var(--border-2); }
.ds-new { border-color: rgba(34, 211, 238, 0.3); background: var(--accent-soft); }
.ds-critical { border-color: rgba(239, 68, 68, 0.4); }
.ds-critical .ds-value { color: var(--color-danger, #ef4444); }
.ds-warning { border-color: rgba(245, 158, 11, 0.4); }
.ds-warning .ds-value { color: #f59e0b; }
.ds-info { border-color: var(--border-1); }
.num-danger { color: var(--color-danger, #ef4444) !important; }
.num-warn { color: #f59e0b !important; }
.ds-card.pulse {
  animation: dpulse 2s ease-in-out infinite;
}
@keyframes dpulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.4); }
  50% { box-shadow: 0 0 0 6px rgba(239, 68, 68, 0); }
}

/* ── Visualisations row ──────────────────────────────────────── */
.drift-vis {
  display: grid;
  grid-template-columns: 1.4fr 1.1fr 1.1fr;
  gap: 10px;
  margin-bottom: 14px;
}
.vis-card {
  background: var(--bg-elev);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  padding: 14px;
  display: flex;
  flex-direction: column;
  min-height: 220px;
}
.vis-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}
.vis-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--text-1);
}
.vis-meta {
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-3);
}

/* trend SVG */
.trend-svg {
  flex: 1;
  width: 100%;
  min-height: 100px;
}
.trend-axis {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: var(--text-3);
  padding: 6px 0 0;
}

/* pie */
.pie-row { display: flex; gap: 16px; align-items: center; flex: 1; }
.pie-svg { width: 110px; height: 110px; flex-shrink: 0; }
.pie-legend { flex: 1; min-width: 0; }
.legend-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-2);
  padding: 3px 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.legend-dot {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  flex-shrink: 0;
}
.legend-label { flex: 1; overflow: hidden; text-overflow: ellipsis; }
.legend-pct { font-size: 10.5px; color: var(--text-3); }
.legend-empty {
  color: var(--text-3);
  font-size: 12px;
  padding: 16px 0;
  text-align: center;
}

/* top devices */
.top-devs { flex: 1; overflow-y: auto; }
.top-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
  border-bottom: 1px dashed var(--border-1);
  font-size: 12px;
}
.top-row:last-child { border-bottom: none; }
.top-rank {
  font-size: 10px;
  color: var(--text-3);
  width: 12px;
  text-align: center;
}
.top-info { flex: 1; min-width: 0; }
.top-name {
  color: var(--text-1);
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.top-vendor { font-size: 10px; color: var(--text-3); }
.top-count {
  font-size: 14px;
  font-weight: 700;
  color: var(--accent);
}
.top-sev {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 3px;
  font-weight: 600;
}
.sev-critical { background: rgba(239, 68, 68, 0.15); color: #ef4444; }
.sev-warning { background: rgba(245, 158, 11, 0.15); color: #f59e0b; }
.sev-info { background: rgba(34, 211, 238, 0.15); color: #22d3ee; }

/* ── Filters + table ─────────────────────────────────────────── */
.drift-filters {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.filter-count {
  margin-left: auto;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-3);
}
.drift-table-wrap {
  background: var(--bg-elev);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  overflow: hidden;
}
.vendor-tag {
  margin-left: 6px;
  font-size: 10px;
  color: var(--text-3);
  background: var(--bg-app);
  border: 1px solid var(--border-1);
  border-radius: 4px;
  padding: 0 5px;
}
.change-hint {
  font-size: 12px;
  color: var(--text-2);
  margin-right: 8px;
}
.diff-stat { font-family: var(--font-mono); font-size: 11px; }
.diff-add { color: #22c55e; margin-right: 4px; }
.diff-del { color: #ef4444; }
.status-new { color: var(--accent); font-weight: 600; }
.status-ack { color: var(--text-3); }

/* Detail drawer */
.dd-root {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow-y: auto;
}
.dd-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border-1);
  flex-shrink: 0;
}
.dd-head-title { display: flex; align-items: center; gap: 10px; }
.dd-icon { font-size: 20px; }
.dd-device { font-size: 15px; font-weight: 700; color: var(--text-1); }
.dd-vendor { font-size: 11px; color: var(--text-3); margin-top: 2px; }
.dd-meta {
  padding: 14px 20px;
  border-bottom: 1px solid var(--border-1);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.dd-meta-row {
  display: flex;
  gap: 12px;
  font-size: 13px;
  color: var(--text-2);
}
.dd-meta-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-3);
  width: 72px;
  flex-shrink: 0;
  padding-top: 1px;
}
.dd-section-title {
  padding: 10px 20px 6px;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 1px;
  color: var(--text-3);
  text-transform: uppercase;
  flex-shrink: 0;
}
.dd-diff {
  flex: 1;
  overflow: auto;
  margin: 0 20px 20px;
  padding: 12px;
  background: var(--bg-app);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 400px;
}
</style>
