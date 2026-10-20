<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, shallowRef, watch, nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import cytoscape, { type Core, type ElementDefinition } from 'cytoscape'
import { api } from '@/api'
import JellyRadioWrapper from '@/components/react/JellyRadioWrapper.vue'

// ─────────────────────────────────────────────────────────────────────
// State
// ─────────────────────────────────────────────────────────────────────

type LabelKey = 'Device' | 'Credential' | 'Document' | 'User' | 'Config'

const stats = ref<{ nodes: Record<string, number>; edges: Record<string, number> } | null>(null)
const label = ref<LabelKey>('Device')
const hops = ref(2)
const data = ref<{ nodes: { id: string; label: string }[]; edges: { source: string; target: string; rel: string }[] }>({ nodes: [], edges: [] })
const loading = ref(false)
const selectedNode = ref<{ id: string; label: string; neighbours?: { direction: 'in' | 'out'; other: string; rel: string }[] } | null>(null)

const viewMode = ref<'grid' | 'graph'>('grid')
const cyContainer = ref<HTMLDivElement | null>(null)
const cyRef = shallowRef<Core | null>(null)

// ─────────────────────────────────────────────────────────────────────
// Palette — one colour per node label so the visualisation reads at a glance
// ─────────────────────────────────────────────────────────────────────

const PALETTE: Record<LabelKey, string> = {
  Device:     '#409EFF',
  Credential: '#67C23A',
  Document:   '#E6A23C',
  User:       '#F56C6C',
  Config:     '#909399',
}

// ─────────────────────────────────────────────────────────────────────
// Data loading
// ─────────────────────────────────────────────────────────────────────

async function loadAll() {
  loading.value = true
  try {
    const [s, sg] = await Promise.all([
      api.graphStats(),
      api.graphSubgraph(label.value, { hops: hops.value }),
    ])
    stats.value = s
    data.value = sg
    await nextTick()
    renderGraph()
  } catch (err: unknown) {
    const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail ?? '加载图谱失败')
  } finally {
    loading.value = false
  }
}

async function rebuild() {
  try {
    await ElMessageBox.confirm(
      '重建图谱会清空图数据库并从 SQL 重新投影所有节点。是否继续？',
      '重建图谱',
      { type: 'warning', confirmButtonText: '重建', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  loading.value = true
  try {
    const r = await api.graphRebuild()
    ElMessage.success(`重建完成：${r.nodes_created} 个节点`)
    await loadAll()
  } catch (err: unknown) {
    ElMessage.error('重建失败：' + ((err as Error).message ?? 'unknown'))
  } finally {
    loading.value = false
  }
}

// ─────────────────────────────────────────────────────────────────────
// Cytoscape render
// ─────────────────────────────────────────────────────────────────────

function buildElements(): ElementDefinition[] {
  const elements: ElementDefinition[] = []
  for (const n of data.value.nodes) {
    elements.push({
      data: { id: n.id, label: n.label },
      classes: `node-${n.label}`,
    })
  }
  for (const e of data.value.edges) {
    elements.push({
      data: { id: `${e.source}-${e.rel}-${e.target}`, source: e.source, target: e.target, rel: e.rel },
      classes: 'edge',
    })
  }
  return elements
}

function renderGraph() {
  if (!cyContainer.value || viewMode.value !== 'graph') return
  if (cyRef.value) {
    cyRef.value.destroy()
    cyRef.value = null
  }
  cyRef.value = cytoscape({
    container: cyContainer.value,
    elements: buildElements(),
    style: [
      // Per-label colours
      ...(Object.entries(PALETTE) as [LabelKey, string][]).map(([k, c]) => ({
        selector: `node.${k.toLowerCase()},node.node-${k}`,
        style: {
          'background-color': c,
          'label': 'data(label)',
          'color': '#fff',
          'text-valign': 'center' as const,
          'text-halign': 'center' as const,
          'font-size': '10px',
          'width': 36,
          'height': 36,
        },
      })),
      {
        selector: 'edge',
        style: {
          'width': 1.5,
          'line-color': '#bbb',
          'target-arrow-color': '#bbb',
          'target-arrow-shape': 'triangle' as const,
          'curve-style': 'bezier' as const,
          'label': 'data(rel)',
          'font-size': '8px',
          'color': '#888',
          'text-rotation': 'autorotate' as const,
        },
      },
    ],
    layout: { name: 'cose', animate: false, padding: 30 },
    wheelSensitivity: 0.2,
  })

  cyRef.value.on('tap', 'node', (ev) => {
    const node = ev.target
    const id = node.id()
    const neighbours: { direction: 'in' | 'out'; other: string; rel: string }[] = []
    node.connectedEdges().forEach((e) => {
      const other = e.source().id() === id ? e.target().id() : e.source().id()
      const direction: 'in' | 'out' = e.source().id() === id ? 'out' : 'in'
      neighbours.push({ direction, other, rel: e.data('rel') })
    })
    selectedNode.value = { id, label: String(node.data('label')), neighbours }
  })
  cyRef.value.on('tap', (ev) => {
    if (ev.target === cyRef.value) selectedNode.value = null
  })
}

watch([label, hops], () => loadAll())
watch(viewMode, () => nextTick(renderGraph))

// ─────────────────────────────────────────────────────────────────────
// Grid rendering helpers
// ─────────────────────────────────────────────────────────────────────

const nodeRows = computed(() => {
  const rows = data.value.nodes.map((n) => ({
    id: n.id,
    label: n.label,
    colour: PALETTE[n.label as LabelKey] ?? '#888',
  }))
  return rows
})

function neighbourCount(id: string): number {
  return data.value.edges.filter((e) => e.source === id || e.target === id).length
}

// ─────────────────────────────────────────────────────────────────────
// Lifecycle
// ─────────────────────────────────────────────────────────────────────

onMounted(loadAll)
onBeforeUnmount(() => {
  cyRef.value?.destroy()
  cyRef.value = null
})
</script>

<template>
  <div class="viz-page">
    <header class="viz-header">
      <div>
        <h2>关系图谱</h2>
        <p class="hint">所有核心实体（设备、凭据、文档、用户、配置）之间的关系网络</p>
      </div>
      <div class="header-actions">
        <JellyRadioWrapper
          v-model="viewMode"
          :items="[
            { value: 'grid', label: '网格' },
            { value: 'graph', label: '关系图' },
          ]"
          aria-label="视图切换"
        />
        <el-button type="warning" :loading="loading" @click="rebuild">重建图谱</el-button>
      </div>
    </header>

    <section class="filters">
      <JellyRadioWrapper
        v-model="label"
        :items="[
          { value: 'Device', label: '设备' },
          { value: 'Credential', label: '凭据' },
          { value: 'Document', label: '文档' },
          { value: 'User', label: '用户' },
          { value: 'Config', label: '配置' },
        ]"
        aria-label="实体类型筛选"
      />
      <el-select v-model="hops" size="default" style="width: 120px; margin-left: 12px">
        <el-option :value="1" label="1 跳" />
        <el-option :value="2" label="2 跳" />
        <el-option :value="3" label="3 跳" />
      </el-select>
    </section>

    <section v-if="stats" class="stats">
      <div class="stat-pill" v-for="(count, key) in stats.nodes" :key="key">
        <span class="dot" :style="{ background: PALETTE[key as LabelKey] ?? '#888' }" />
        <span class="label">{{ key }}</span>
        <span class="count">{{ count }}</span>
      </div>
      <div class="stat-divider">关系</div>
      <div class="stat-pill edge" v-for="(count, key) in stats.edges" :key="key">
        <span class="rel">{{ key }}</span>
        <span class="count">{{ count }}</span>
      </div>
    </section>

    <section v-if="viewMode === 'grid'" class="grid">
      <div v-if="loading && nodeRows.length === 0" class="empty">加载中…</div>
      <div v-else-if="nodeRows.length === 0" class="empty">
        当前没有 {{ label }} 类型节点。先到对应模块创建数据，然后点「重建图谱」。
      </div>
      <el-card
        v-for="row in nodeRows"
        :key="row.id"
        class="node-card"
        shadow="hover"
        @click="selectedNode = { id: row.id, label: row.label }"
      >
        <div class="node-card-header">
          <span class="dot" :style="{ background: row.colour }" />
          <strong>{{ row.id }}</strong>
        </div>
        <div class="node-card-meta">
          <el-tag size="small" :style="{ background: row.colour, color: '#fff', border: 'none' }">
            {{ row.label }}
          </el-tag>
          <span class="neighbours">{{ neighbourCount(row.id) }} 条关系</span>
        </div>
      </el-card>
    </section>

    <section v-else class="graph-wrapper">
      <div v-if="loading && data.nodes.length === 0" class="empty">加载中…</div>
      <div v-else-if="data.nodes.length === 0" class="empty">
        当前没有 {{ label }} 类型节点。先到对应模块创建数据，然后点「重建图谱」。
      </div>
      <div v-show="data.nodes.length > 0" ref="cyContainer" class="cy-container" />
    </section>

    <el-drawer
      v-model="selectedNode"
      :show-close="true"
      direction="rtl"
      size="360px"
      title="节点详情"
    >
      <template v-if="selectedNode">
        <div class="detail-header">
          <span class="dot" :style="{ background: PALETTE[selectedNode.label as LabelKey] ?? '#888' }" />
          <span class="id">{{ selectedNode.id }}</span>
          <el-tag size="small">{{ selectedNode.label }}</el-tag>
        </div>
        <h4>关联 ({{ selectedNode.neighbours?.length ?? 0 }})</h4>
        <ul v-if="selectedNode.neighbours && selectedNode.neighbours.length > 0" class="neighbours">
          <li v-for="(n, idx) in selectedNode.neighbours" :key="idx">
            <el-tag :type="n.direction === 'out' ? 'success' : 'info'" size="small">
              {{ n.direction === 'out' ? '→' : '←' }} {{ n.rel }}
            </el-tag>
            <span class="other-id">{{ n.other }}</span>
          </li>
        </ul>
        <p v-else class="empty">此节点暂无关联。</p>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.viz-page { padding: 16px 24px 32px; }
.viz-header {
  display: flex; justify-content: space-between; align-items: flex-end;
  margin-bottom: 16px; flex-wrap: wrap; gap: 12px;
}
.viz-header h2 { margin: 0; font-size: 20px; }
.viz-header .hint { color: #909399; margin: 4px 0 0; font-size: 13px; }
.header-actions { display: flex; gap: 12px; }
.filters { margin-bottom: 12px; display: flex; align-items: center; }
.stats {
  display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
  padding: 10px 12px; background: #f5f7fa; border-radius: 6px; margin-bottom: 16px;
}
.stat-pill {
  display: inline-flex; align-items: center; gap: 6px;
  background: #fff; border: 1px solid #ebeef5; border-radius: 14px; padding: 2px 10px;
  font-size: 12px;
}
.stat-pill .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.stat-pill .count { font-weight: 600; color: #303133; }
.stat-divider { color: #909399; font-size: 12px; padding: 0 4px; }
.stat-pill.edge { background: #fafbfc; }
.stat-pill.edge .rel { color: #606266; }
.grid {
  display: grid; gap: 12px;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
}
.node-card { cursor: pointer; transition: transform .15s; }
.node-card:hover { transform: translateY(-2px); }
.node-card-header { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.node-card-header .dot { width: 10px; height: 10px; border-radius: 50%; }
.node-card-meta { display: flex; justify-content: space-between; align-items: center; }
.neighbours { color: #909399; font-size: 12px; }
.graph-wrapper { height: calc(100vh - 280px); min-height: 400px; position: relative; }
.cy-container { width: 100%; height: 100%; background: #fafafa; border-radius: 6px; border: 1px solid #ebeef5; }
.empty { padding: 40px; text-align: center; color: #909399; }
.detail-header { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
.detail-header .dot { width: 14px; height: 14px; border-radius: 50%; }
.detail-header .id { font-weight: 600; font-family: 'Cascadia Code', monospace; }
.neighbours { list-style: none; padding: 0; margin: 0; }
.neighbours li { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px dashed #ebeef5; }
.other-id { font-family: 'Cascadia Code', monospace; font-size: 12px; color: #606266; }
</style>
