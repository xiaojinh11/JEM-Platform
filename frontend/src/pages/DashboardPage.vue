<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, type DashboardStats, type DashboardEvent, type DashboardJob } from '@/api'

const router = useRouter()
const stats = ref<DashboardStats | null>(null)
const events = ref<DashboardEvent[]>([])
const jobs = ref<DashboardJob[]>([])
const loading = ref(false)
let pollTimer: number | null = null

// Adaptive polling: slow down when no active jobs, speed up when jobs are running
const BASE_INTERVAL_MS = 30_000  // 30s when idle
const ACTIVE_INTERVAL_MS = 5_000  // 5s when jobs are active
const CRITICAL_INTERVAL_MS = 2_000  // 2s when critical events pending

const currentInterval = computed(() => {
  if (!stats.value) return BASE_INTERVAL_MS
  const hasCritical = (stats.value.pending_approval ?? 0) > 0
  const hasActiveJobs = jobs.value.some(j => j.status === 'running' || j.status === 'queued')
  if (hasCritical) return CRITICAL_INTERVAL_MS
  if (hasActiveJobs) return ACTIVE_INTERVAL_MS
  return BASE_INTERVAL_MS
})

const refresh = async () => {
  try {
    const [s, e, j] = await Promise.all([
      api.dashboard(),
      api.recentEvents(),
      api.activeJobs(),
    ])
    stats.value = s
    events.value = e
    jobs.value = j
  } catch (err) {
    // ignore transient failures during polling
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  loading.value = true
  await refresh()
  const schedule = () => {
    pollTimer = window.setTimeout(async () => {
      await refresh()
      schedule()  // re-read interval (could change between polls)
    }, currentInterval.value)
  }
  schedule()
})
onBeforeUnmount(() => {
  if (pollTimer) clearTimeout(pollTimer)
})

/* === derived === */
const vendorHealth = computed(() => {
  const byVendor = stats.value?.by_vendor || {}
  const total = stats.value?.total_devices || 1
  return Object.entries(byVendor).map(([vendor, count]) => ({
    vendor,
    count,
    pct: Math.round((count / total) * 100),
  }))
})

function statusTone(s: string): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
  if (s === 'succeeded') return 'success'
  if (s === 'failed') return 'danger'
  if (s === 'needs_verification') return 'warning'
  if (s === 'running') return 'primary'
  if (s === 'pending') return 'info'
  if (s === 'canceled') return 'info'
  return 'info'
}

function actionTone(a: string): 'success' | 'warning' | 'danger' | 'info' | 'primary' {
  if (a.startsWith('login')) return 'primary'
  if (a.includes('approve')) return 'success'
  if (a.includes('reject') || a.includes('fail') || a.includes('delete')) return 'danger'
  if (a.includes('update')) return 'warning'
  return 'info'
}

function fmtTime(iso?: string | null) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function fmtRel(iso?: string | null) {
  if (!iso) return ''
  const d = new Date(iso).getTime()
  const diff = Math.floor((Date.now() - d) / 1000)
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

function vendorShort(v: string) {
  const map: Record<string, string> = {
    huawei_vrp: 'HW',
    cisco_ios_xe: 'CISCO',
    ruijie_rgos: 'RUIJIE',
    simulated: 'SIM',
  }
  return map[v] || v.slice(0, 4).toUpperCase()
}
</script>

<template>
  <div class="dash" v-loading="loading">

    <!-- Row 1: single-line metric bar (no purple hero) -->
    <section class="metrics panel">
      <div class="metric">
        <div class="m-label">DEVICES · 在线 / 总数</div>
        <div class="m-value mono">
          <span class="num">{{ stats?.active_devices ?? 0 }}</span>
          <span class="sep">/</span>
          <span class="dim">{{ stats?.total_devices ?? 0 }}</span>
        </div>
        <div class="m-sub mono">SIM {{ stats?.simulated_devices ?? 0 }}</div>
      </div>
      <div class="metric">
        <div class="m-label">REVISIONS · 配置快照</div>
        <div class="m-value mono">{{ stats?.revisions_total ?? 0 }}</div>
        <div class="m-sub mono">GIT-LIKE ARCHIVE</div>
      </div>
      <div class="metric">
        <div class="m-label">JOBS · 24h 状态</div>
        <div class="m-value mono">{{ stats?.jobs_by_status?.succeeded ?? 0 }}<span class="dim">/{{ stats?.total_jobs ?? 0 }}</span></div>
        <div class="m-sub">
          <span class="dot dot-ok"></span><span class="mono">{{ stats?.jobs_by_status?.succeeded ?? 0 }}</span>
          <span class="dot dot-fail" style="margin-left:8px"></span><span class="mono">{{ stats?.jobs_by_status?.failed ?? 0 }}</span>
        </div>
      </div>
      <div class="metric">
        <div class="m-label">CREDENTIALS · 加密条目</div>
        <div class="m-value mono">{{ stats?.total_credentials ?? 0 }}</div>
        <div class="m-sub mono">FERNET</div>
      </div>
      <div class="metric">
        <div class="m-label">USERS · 平台用户</div>
        <div class="m-value mono">{{ stats?.total_users ?? 0 }}</div>
        <div class="m-sub mono">RBAC ENABLED</div>
      </div>
      <div class="metric" :class="{ alert: (stats?.pending_approval ?? 0) > 0 }">
        <div class="m-label">PENDING APPROVAL · 待审批</div>
        <div class="m-value mono">{{ stats?.pending_approval ?? 0 }}</div>
        <div class="m-sub mono">HIGH-RISK JOBS</div>
      </div>
    </section>

    <!-- Row 2: radar + recent changes -->
    <section class="row-2">
      <!-- Vendor health radar (inline SVG) -->
      <div class="panel col-radar">
        <div class="panel-header">
          <span>网络健康度 · 厂商分布</span>
          <span class="mono dim">N={{ stats?.total_devices ?? 0 }}</span>
        </div>
        <div class="panel-body radar-body">
          <svg v-if="vendorHealth.length" viewBox="0 0 320 240" class="radar">
            <!-- axes -->
            <g stroke="#1f2a44" fill="none">
              <line x1="160" y1="120" x2="160" y2="20" />
              <line x1="160" y1="120" x2="252" y2="60" />
              <line x1="160" y1="120" x2="252" y2="180" />
              <line x1="160" y1="120" x2="160" y2="220" />
              <line x1="160" y1="120" x2="68" y2="180" />
              <line x1="160" y1="120" x2="68" y2="60" />
              <circle cx="160" cy="120" r="40" />
              <circle cx="160" cy="120" r="80" />
              <circle cx="160" cy="120" r="100" />
            </g>
            <!-- polygon -->
            <polygon
              :points="vendorHealth.map((v, i) => {
                const angle = (Math.PI * 2 * i) / vendorHealth.length - Math.PI / 2
                const r = 100 * (v.pct / 100)
                return `${(160 + r * Math.cos(angle)).toFixed(1)},${(120 + r * Math.sin(angle)).toFixed(1)}`
              }).join(' ')"
              fill="rgba(34,211,238,0.18)"
              stroke="#22d3ee"
              stroke-width="1.5"
            />
            <!-- dots + labels -->
            <g>
              <g v-for="(v, i) in vendorHealth" :key="v.vendor">
                <circle
                  :cx="160 + 100 * (v.pct / 100) * Math.cos((Math.PI * 2 * i) / vendorHealth.length - Math.PI / 2)"
                  :cy="120 + 100 * (v.pct / 100) * Math.sin((Math.PI * 2 * i) / vendorHealth.length - Math.PI / 2)"
                  r="3"
                  fill="#22d3ee"
                />
                <text
                  :x="160 + 115 * Math.cos((Math.PI * 2 * i) / vendorHealth.length - Math.PI / 2)"
                  :y="120 + 115 * Math.sin((Math.PI * 2 * i) / vendorHealth.length - Math.PI / 2) + 4"
                  text-anchor="middle"
                  fill="#94a3b8"
                  font-family="JetBrains Mono, monospace"
                  font-size="10"
                >
                  {{ vendorShort(v.vendor) }} · {{ v.count }}
                </text>
              </g>
            </g>
          </svg>
          <div v-else class="empty mono">NO DATA · 添加设备后查看分布</div>
        </div>
      </div>

      <!-- Recent changes feed -->
      <div class="panel col-events">
        <div class="panel-header">
          <span>最近变更 · Recent Changes</span>
          <span class="mono dim">{{ events.length }}</span>
        </div>
        <ul class="event-list">
          <li v-for="ev in events" :key="ev.id" class="event-item">
            <el-tag size="small" :type="actionTone(ev.action)" effect="dark">{{ ev.action }}</el-tag>
            <div class="event-body">
              <div class="event-detail">
                <span class="actor mono">{{ ev.actor }}</span>
                <span class="dim"> · </span>
                <span>{{ ev.detail || `${ev.target_kind || ''} ${ev.target_id || ''}`.trim() || '—' }}</span>
              </div>
              <div class="event-time mono dim">{{ fmtTime(ev.created_at) }} · {{ fmtRel(ev.created_at) }}</div>
            </div>
          </li>
          <li v-if="!events.length" class="empty mono">NO EVENTS</li>
        </ul>
      </div>
    </section>

    <!-- Row 3: pipeline -->
    <section class="panel col-pipeline">
      <div class="panel-header">
        <span>作业流水线 · Active Pipeline</span>
        <el-button size="small" text @click="router.push('/jobs')">查看全部 →</el-button>
      </div>
      <div class="panel-body">
        <div v-if="!jobs.length" class="empty mono">QUEUE IDLE · 当前无运行任务</div>
        <div v-else class="pipeline">
          <div v-for="j in jobs" :key="j.id" class="pipe-row">
            <div class="pipe-meta">
              <div class="pipe-name mono">
                #{{ j.id }} {{ j.name }}
                <el-tag v-if="j.requires_approval" size="small" type="warning" effect="dark" style="margin-left:6px">HIGH-RISK</el-tag>
              </div>
              <div class="pipe-sub mono dim">
                <span>{{ j.kind }}</span>
                <span style="margin-left:8px">by {{ j.initiated_by }}</span>
                <span style="margin-left:8px">{{ j.target_device_ids.length }} devices</span>
              </div>
            </div>
            <div class="pipe-progress">
              <div class="pipe-track">
                <div
                  class="pipe-fill"
                  :class="statusTone(j.status)"
                  :style="{ width: (j.progress || 0) + '%' }"
                ></div>
              </div>
              <span class="pipe-pct mono">{{ j.progress || 0 }}%</span>
              <el-tag size="small" :type="statusTone(j.status)" effect="dark">{{ j.status }}</el-tag>
            </div>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.dash {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* ===== Row 1: single-line metrics ===== */
.metrics {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  padding: 0;
  overflow: hidden;
}
.metric {
  padding: 12px 14px;
  border-right: 1px solid var(--border-1);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.metric:last-child { border-right: none; }
.m-label {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-3);
  letter-spacing: 0.6px;
  text-transform: uppercase;
}
.m-value {
  font-size: 22px;
  font-weight: 600;
  color: var(--text-1);
  line-height: 1.1;
  font-feature-settings: 'tnum' 1;
}
.m-value .sep { color: var(--text-4); margin: 0 2px; }
.m-value .num { color: var(--accent); }
.m-sub {
  font-size: 11px;
  color: var(--text-3);
  letter-spacing: 0.4px;
}
.metric.alert .m-value { color: var(--warning); }

/* ===== Row 2: 2-col ===== */
.row-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}
.col-radar .panel-body { padding: 14px; }
.radar-body {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 240px;
}
.radar { width: 100%; height: auto; max-width: 320px; }
.empty {
  font-size: 12px;
  color: var(--text-3);
  text-align: center;
  padding: 40px 0;
}

.col-events .panel-body, .col-events { display: flex; flex-direction: column; }
.event-list {
  list-style: none;
  margin: 0;
  padding: 0;
  flex: 1;
  overflow-y: auto;
  max-height: 280px;
}
.event-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border-1);
}
.event-item:last-child { border-bottom: none; }
.event-item:hover { background: var(--bg-elev); }
.event-body { flex: 1; min-width: 0; }
.event-detail {
  font-size: 12.5px;
  color: var(--text-1);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.actor { color: var(--accent); font-weight: 600; }
.event-time {
  font-size: 10.5px;
  color: var(--text-3);
  margin-top: 2px;
}

/* ===== Row 3: pipeline ===== */
.col-pipeline .panel-body { padding: 0; }
.pipeline { display: flex; flex-direction: column; }
.pipe-row {
  display: grid;
  grid-template-columns: 1fr 320px;
  align-items: center;
  gap: 16px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border-1);
}
.pipe-row:last-child { border-bottom: none; }
.pipe-name {
  font-size: 13px;
  color: var(--text-1);
  font-weight: 600;
}
.pipe-sub { font-size: 11px; margin-top: 3px; }
.pipe-progress {
  display: flex;
  align-items: center;
  gap: 10px;
}
.pipe-track {
  flex: 1;
  height: 6px;
  background: var(--bg-input);
  border-radius: 3px;
  overflow: hidden;
  border: 1px solid var(--border-1);
}
.pipe-fill {
  height: 100%;
  background: var(--accent);
  transition: width 0.5s ease;
}
.pipe-fill.success { background: var(--success); }
.pipe-fill.warning { background: var(--warning); }
.pipe-fill.danger  { background: var(--danger); }
.pipe-pct {
  font-size: 11px;
  color: var(--text-2);
  width: 40px;
  text-align: right;
}

@media (max-width: 1200px) {
  .metrics { grid-template-columns: repeat(3, 1fr); }
  .row-2 { grid-template-columns: 1fr; }
  .pipe-row { grid-template-columns: 1fr; }
}
</style>
