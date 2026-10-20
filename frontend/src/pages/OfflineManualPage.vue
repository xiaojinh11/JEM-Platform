<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import type { ElTree } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowDown, ArrowUp, Connection, Document, Expand, Folder, FolderOpened, Position, Refresh, Search, WarningFilled } from '@element-plus/icons-vue'
import { api } from '@/api'

interface ManualNode {
  id: string
  label: string
  path: string | null
  children?: ManualNode[]
}

interface ManualHealth {
  score: number
  grade: 'ok' | 'minor' | 'warn' | 'broken'
  references_total: number
  references_missing: number
  references_case_only: number
  missing_targets: Array<[string, number]>
  case_only_targets: Array<[string, number]>
  top_missing_sources: Array<[string, number]>
}

interface ManualManifest {
  title: string
  product_version?: string | null
  document_version?: string | null
  issue_date?: string | null
  language?: string | null
  product_type?: string | null
  base_url: string
  home_url: string
  tree: ManualNode[]
  health?: ManualHealth | null
}

interface FlatEntry {
  node: ManualNode
  chain: string[]
}

const route = useRoute()
const router = useRouter()

const loading = ref(true)
const manifest = ref<ManualManifest | null>(null)
const frameUrl = ref('')
const tocFilter = ref('')
const treeRef = ref<InstanceType<typeof ElTree> | null>(null)
const searchTerm = ref('')
const searchHits = ref<FlatEntry[]>([])
const currentId = ref<string>('')
const healthDrawerOpen = ref(false)

const manualId = computed(() => Number(route.params.id))
const treeProps = { children: 'children', label: 'label' }

const health = computed<ManualHealth | null>(() => manifest.value?.health ?? null)
const healthGrade = computed(() => health.value?.grade ?? 'ok')
const healthBadgeType = computed<'success' | 'info' | 'warning' | 'danger'>(() => {
  switch (healthGrade.value) {
    case 'ok': return 'success'
    case 'minor': return 'info'
    case 'warn': return 'warning'
    default: return 'danger'
  }
})
const healthLabel = computed(() => {
  switch (healthGrade.value) {
    case 'ok': return '包体完整'
    case 'minor': return '轻微瑕疵'
    case 'warn': return '部分缺失'
    case 'broken': return '包体异常'
    default: return '健康度未知'
  }
})

const assetUrl = (path: string) => {
  const encoded = path.split('/').map(encodeURIComponent).join('/')
  return `${manifest.value?.base_url}/${encoded}`
}

function flatten(nodes: ManualNode[] | undefined, chain: string[] = []): FlatEntry[] {
  if (!nodes) return []
  const out: FlatEntry[] = []
  for (const n of nodes) {
    const here = [...chain, n.label]
    if (n.path) out.push({ node: n, chain: here })
    if (n.children) out.push(...flatten(n.children, here))
  }
  return out
}

const flatList = computed<FlatEntry[]>(() => (manifest.value ? flatten(manifest.value.tree) : []))
const topLevelIds = computed(() => manifest.value?.tree.map((n) => n.id) ?? [])

const currentIndex = computed(() => flatList.value.findIndex((e) => e.node.id === currentId.value))
const currentEntry = computed<FlatEntry | null>(() => flatList.value[currentIndex.value] ?? null)
const breadcrumb = computed<string[]>(() => currentEntry.value?.chain ?? [])
const prevEntry = computed<FlatEntry | null>(() => flatList.value[currentIndex.value - 1] ?? null)
const nextEntry = computed<FlatEntry | null>(() => flatList.value[currentIndex.value + 1] ?? null)

const filterNode = (value: string, data: ManualNode) => {
  if (!value) return true
  return data.label.toLowerCase().includes(value.toLowerCase())
}

watch(tocFilter, (value) => treeRef.value?.filter(value))

watch(currentId, async (id) => {
  if (!id || !manifest.value) return
  await nextTick()
  treeRef.value?.setCurrentKey(id)
})

function openNode(node: ManualNode) {
  // Folder-only node (no URL): toggle expand/collapse on click.
  if (!node.path) {
    const isOpen = treeRef.value?.getNode(node)?.expanded
    if (isOpen !== undefined) treeRef.value?.expandNode(node, !isOpen, false)
    return
  }
  currentId.value = node.id
  frameUrl.value = assetUrl(node.path)
}

function gotoEntry(entry: FlatEntry | null) {
  if (entry?.node.path) openNode(entry.node)
}

function expandAll() {
  if (!manifest.value || !treeRef.value) return
  const all: ManualNode[] = []
  const walk = (ns: ManualNode[]) => ns.forEach((n) => {
    if (n.children?.length) {
      all.push(n)
      walk(n.children)
    }
  })
  walk(manifest.value.tree)
  all.forEach((n) => treeRef.value?.expandNode(n, true, false))
}

function collapseAll() {
  if (!manifest.value || !treeRef.value) return
  const walk = (ns: ManualNode[]) => ns.forEach((n) => {
    if (n.children?.length) {
      treeRef.value?.expandNode(n, false, false)
      walk(n.children)
    }
  })
  walk(manifest.value.tree)
}

function runSearch() {
  const term = searchTerm.value.trim()
  if (!term) {
    searchHits.value = []
    return
  }
  const needle = term.toLowerCase()
  searchHits.value = flatList.value.filter((e) => e.node.label.toLowerCase().includes(needle)).slice(0, 50)
}

watch(searchTerm, () => runSearch())

function jumpToHit(hit: FlatEntry) {
  openNode(hit.node)
  treeRef.value?.scrollIntoView(hit.node)
}

function reload() {
  const node = currentEntry.value?.node
  if (node?.path) frameUrl.value = assetUrl(node.path)
}

onMounted(async () => {
  try {
    manifest.value = (await api.createOfflineManualSession(manualId.value)) as ManualManifest
    // Open the first leaf by default so the right pane has content immediately.
    const firstLeaf = flatList.value[0]?.node
    if (firstLeaf?.path) openNode(firstLeaf)
    else frameUrl.value = manifest.value.home_url
  } catch (err: unknown) {
    const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail || '无法打开原生离线手册')
    router.replace('/documents')
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="manual-page" v-loading="loading">
    <header class="manual-header" v-if="manifest">
      <div class="header-left">
        <button class="back" @click="router.back()">
          <el-icon><Position /></el-icon>
          <span>手册库</span>
        </button>
        <div class="title-block">
          <h1>{{ manifest.title }}</h1>
          <div class="meta">
            <el-tag size="small" type="info" effect="plain" v-if="manifest.product_type">
              {{ manifest.product_type }}
            </el-tag>
            <span>产品版本 <b>{{ manifest.product_version || '—' }}</b></span>
            <span>文档版本 <b>{{ manifest.document_version || '—' }}</b></span>
            <span>发行日期 <b>{{ manifest.issue_date || '—' }}</b></span>
            <span>语言 <b>{{ manifest.language || '—' }}</b></span>
          </div>
        </div>
      </div>
      <div class="header-right">
        <el-input v-model="searchTerm" placeholder="在手册中搜索章节…" clearable class="search">
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <span class="native-badge">NATIVE OFFLINE MANUAL</span>
        <button
          v-if="health"
          class="health-badge"
          :class="`grade-${healthGrade}`"
          type="button"
          @click="healthDrawerOpen = true"
          :title="`${healthLabel} — 点击查看详情`"
        >
          <el-icon><WarningFilled /></el-icon>
          <span>{{ healthLabel }}</span>
          <em>{{ health.score }} 分</em>
        </button>
      </div>
    </header>

    <el-drawer
      v-model="healthDrawerOpen"
      title="包体健康度"
      direction="rtl"
      size="420px"
      :with-header="true"
    >
      <template v-if="health">
        <div class="health-summary" :class="`grade-${healthGrade}`">
          <div class="health-score">{{ health.score }}</div>
          <div class="health-state">
            <h3>{{ healthLabel }}</h3>
            <p>
              共扫描 <b>{{ health.references_total.toLocaleString() }}</b> 处引用，
              其中 <b>{{ health.references_missing.toLocaleString() }}</b> 个真实缺失、
              <b>{{ health.references_case_only.toLocaleString() }}</b> 个仅大小写不匹配。
            </p>
            <p v-if="health.references_case_only" class="hint">
              这些资源在 zip 中存在但大小写不一致；多数 HTTP 客户端会因大小写差异直接报 404，
              需要厂商重新打包时修正。
            </p>
          </div>
        </div>

        <section v-if="health.references_missing" class="health-section">
          <h4>
            真实缺失资源
            <el-tag size="small" type="danger">{{ health.references_missing }}</el-tag>
          </h4>
          <p v-if="!health.missing_targets.length" class="hint">
            没有具体目标记录 — 大概率是 vendor HTML 写了不存在的文件名。
          </p>
          <ul v-else class="health-list">
            <li v-for="[target, count] in health.missing_targets" :key="target">
              <code>{{ target }}</code>
              <span class="count">×{{ count }}</span>
            </li>
          </ul>
        </section>

        <section v-if="health.references_case_only" class="health-section">
          <h4>
            大小写不匹配
            <el-tag size="small" type="warning">{{ health.references_case_only }}</el-tag>
          </h4>
          <ul class="health-list">
            <li v-for="[target, count] in health.case_only_targets" :key="target">
              <code>{{ target }}</code>
              <span class="count">×{{ count }}</span>
            </li>
          </ul>
        </section>

        <section v-if="health.top_missing_sources.length" class="health-section">
          <h4>缺失引用最多的章节</h4>
          <ul class="health-list">
            <li v-for="[src, count] in health.top_missing_sources" :key="src">
              <code>{{ src }}</code>
              <span class="count">×{{ count }}</span>
            </li>
          </ul>
        </section>

        <div v-if="!health.references_missing && !health.references_case_only" class="health-empty">
          <el-icon><WarningFilled /></el-icon>
          <p>本手册的所有内部引用均能命中 zip 资源。</p>
        </div>
      </template>
    </el-drawer>

    <div v-if="searchHits.length" class="search-hits">
      <div class="hits-title">
        搜索 "<b>{{ searchTerm }}</b>" 共 <b>{{ searchHits.length }}</b> 条结果
      </div>
      <div class="hits-list">
        <button v-for="(h, i) in searchHits" :key="i" class="hit" @click="jumpToHit(h)">
          <div class="hit-label">{{ h.node.label }}</div>
          <div class="hit-chain">{{ h.chain.join(' / ') }}</div>
        </button>
      </div>
    </div>

    <div class="manual-shell" v-if="manifest">
      <aside class="toc">
        <div class="toc-title">
          <div class="toc-title-row">
            <span><el-icon><Folder /></el-icon> 目录</span>
            <span class="toc-stat">{{ flatList.length }} 个章节</span>
          </div>
          <el-input v-model="tocFilter" size="small" clearable placeholder="筛选章节…" />
          <div class="toc-actions">
            <el-button size="small" plain @click="expandAll">
              <el-icon><Expand /></el-icon> 全部展开
            </el-button>
            <el-button size="small" plain @click="collapseAll">全部折叠</el-button>
          </div>
        </div>
        <div class="toc-scroll">
          <el-tree
            ref="treeRef"
            :data="manifest.tree"
            :props="treeProps"
            node-key="id"
            :filter-node-method="filterNode"
            :default-expanded-keys="topLevelIds"
            highlight-current
            :render-after-expand="false"
            @node-click="openNode"
          >
            <template #default="{ data }">
              <span class="tree-row">
                <template v-if="data.children && data.children.length">
                  <el-icon><FolderOpened /></el-icon>
                </template>
                <template v-else>
                  <el-icon><Document /></el-icon>
                </template>
                <span class="tree-label">{{ data.label }}</span>
              </span>
            </template>
          </el-tree>
        </div>
      </aside>

      <main class="reader">
        <div class="reader-bar">
          <div class="crumbs">
            <template v-for="(seg, i) in breadcrumb" :key="i">
              <span v-if="i > 0" class="crumb-sep">›</span>
              <span :class="i === breadcrumb.length - 1 ? 'crumb-leaf' : 'crumb-mid'">{{ seg }}</span>
            </template>
          </div>
          <div class="reader-actions">
            <el-button :disabled="!prevEntry?.node.path" size="small" @click="gotoEntry(prevEntry)">
              <el-icon><ArrowUp /></el-icon> 上一章
            </el-button>
            <el-button :disabled="!nextEntry?.node.path" size="small" @click="gotoEntry(nextEntry)">
              下一章 <el-icon><ArrowDown /></el-icon>
            </el-button>
            <el-button size="small" plain @click="reload">
              <el-icon><Refresh /></el-icon> 重载
            </el-button>
          </div>
        </div>
        <div class="reader-meta" v-if="currentEntry">
          <span class="reader-title">{{ currentEntry.node.label }}</span>
          <span class="reader-pos">
            <el-icon><Connection /></el-icon>
            章节 {{ currentIndex + 1 }} / {{ flatList.length }}
          </span>
        </div>
        <iframe
          v-if="frameUrl"
          :src="frameUrl"
          title="厂商原生离线手册"
          sandbox="allow-same-origin allow-downloads"
          referrerpolicy="no-referrer"
        />
      </main>
    </div>
  </div>
</template>

<style scoped>
.manual-page { height: calc(100vh - 32px); display: flex; flex-direction: column; gap: 12px; }

.manual-header {
  display: flex;
  align-items: stretch;
  justify-content: space-between;
  gap: 24px;
  padding: 14px 20px;
  background: var(--bg-canvas);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
}
.header-left { display: flex; gap: 16px; align-items: center; min-width: 0; flex: 1; }
.back {
  border: 1px solid var(--border-1);
  background: var(--bg-canvas);
  color: var(--text-2);
  cursor: pointer;
  padding: 6px 12px;
  border-radius: 999px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  white-space: nowrap;
}
.back:hover { color: var(--accent); border-color: var(--accent); }
.title-block { min-width: 0; flex: 1; }
h1 { margin: 0 0 6px; font-size: 20px; color: var(--text-1); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.meta { display: flex; flex-wrap: wrap; gap: 14px; color: var(--text-3); font-size: 12px; align-items: center; }
.meta b { color: var(--text-2); font-weight: 600; }

.header-right { display: flex; align-items: center; gap: 12px; }
.search { width: 320px; }
.native-badge {
  color: var(--accent);
  border: 1px solid var(--accent);
  border-radius: 4px;
  padding: 6px 10px;
  font: 10px var(--font-mono);
  letter-spacing: .5px;
  white-space: nowrap;
}

.health-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border-radius: 16px;
  padding: 6px 12px;
  font: 12px var(--font-sans);
  cursor: pointer;
  border: 1px solid transparent;
  background: transparent;
  transition: transform .15s ease, background .15s ease;
}
.health-badge:hover { transform: translateY(-1px); }
.health-badge em { font-style: normal; font-weight: 700; }
.health-badge.grade-ok { background: rgba(28, 175, 110, .12); border-color: rgba(28, 175, 110, .4); color: #1caf6e; }
.health-badge.grade-minor { background: rgba(86, 158, 240, .12); border-color: rgba(86, 158, 240, .4); color: #569ef0; }
.health-badge.grade-warn { background: rgba(230, 162, 60, .14); border-color: rgba(230, 162, 60, .45); color: #c97a14; }
.health-badge.grade-broken { background: rgba(245, 108, 108, .14); border-color: rgba(245, 108, 108, .45); color: #f56c6c; }

.health-summary {
  display: flex;
  gap: 16px;
  align-items: center;
  padding: 12px 14px;
  border-radius: 10px;
  margin-bottom: 16px;
}
.health-summary.grade-ok { background: rgba(28, 175, 110, .08); }
.health-summary.grade-minor { background: rgba(86, 158, 240, .08); }
.health-summary.grade-warn { background: rgba(230, 162, 60, .10); }
.health-summary.grade-broken { background: rgba(245, 108, 108, .10); }
.health-score {
  font: 700 32px var(--font-sans);
  line-height: 1;
}
.health-summary.grade-ok .health-score { color: #1caf6e; }
.health-summary.grade-minor .health-score { color: #569ef0; }
.health-summary.grade-warn .health-score { color: #c97a14; }
.health-summary.grade-broken .health-score { color: #f56c6c; }
.health-state h3 { margin: 0 0 4px; font-size: 14px; }
.health-state p { margin: 4px 0; color: var(--text-2); font-size: 12px; line-height: 1.6; }
.health-state .hint { color: var(--text-3); }

.health-section { margin-bottom: 18px; }
.health-section h4 {
  display: flex; align-items: center; gap: 8px;
  margin: 0 0 8px; font-size: 13px; color: var(--text-1);
}
.health-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex; flex-direction: column; gap: 4px;
}
.health-list li {
  display: flex; justify-content: space-between; align-items: center;
  padding: 6px 8px;
  background: var(--bg-canvas);
  border: 1px solid var(--border-1);
  border-radius: 4px;
  font-size: 12px;
}
.health-list code {
  font: 11px var(--font-mono);
  color: var(--text-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 78%;
}
.health-list .count {
  color: var(--text-3);
  font: 11px var(--font-mono);
  flex-shrink: 0;
}
.health-empty {
  text-align: center;
  color: var(--text-2);
  padding: 30px 0;
}
.health-empty p { margin-top: 8px; font-size: 13px; }

.search-hits {
  background: var(--bg-canvas);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  padding: 10px 16px 12px;
  max-height: 200px;
  overflow: auto;
}
.hits-title { font-size: 12px; color: var(--text-3); margin-bottom: 8px; }
.hits-title b { color: var(--accent); }
.hits-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 8px; }
.hit {
  text-align: left;
  border: 1px solid var(--border-1);
  background: var(--bg-canvas);
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  transition: border-color .15s ease;
}
.hit:hover { border-color: var(--accent); }
.hit-label { font-weight: 600; color: var(--text-1); font-size: 13px; }
.hit-chain { color: var(--text-3); font-size: 11px; margin-top: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.manual-shell { flex: 1; min-height: 0; display: grid; grid-template-columns: 320px 1fr; gap: 12px; }
.toc, .reader { background: var(--bg-canvas); border: 1px solid var(--border-1); border-radius: var(--radius-md); overflow: hidden; display: flex; flex-direction: column; }

.toc-title { display: grid; gap: 10px; padding: 14px 16px; font-weight: 700; color: var(--text-1); border-bottom: 1px solid var(--border-1); }
.toc-title-row { display: flex; align-items: center; justify-content: space-between; }
.toc-stat { color: var(--text-3); font-size: 11px; font-weight: 500; }
.toc-actions { display: flex; gap: 8px; }
.toc-scroll { flex: 1; overflow-y: auto; padding: 8px; }
.toc-scroll :deep(.el-tree) { background: transparent; color: var(--text-2); }
.toc-scroll :deep(.el-tree-node__content) { min-height: 30px; height: auto; padding: 3px 0; align-items: flex-start; }
.toc-scroll :deep(.el-tree-node__label) { white-space: normal; line-height: 1.4; font-size: 12px; }
.tree-row { display: inline-flex; align-items: center; gap: 6px; }
.tree-label { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 240px; }

.reader { min-width: 0; }
.reader-bar { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 16px; border-bottom: 1px solid var(--border-1); }
.crumbs { color: var(--text-3); font-size: 12px; display: flex; flex-wrap: wrap; align-items: center; gap: 4px; min-width: 0; }
.crumb-sep { color: var(--border-1); margin: 0 4px; }
.crumb-mid { color: var(--text-3); }
.crumb-leaf { color: var(--text-1); font-weight: 600; }
.reader-actions { display: flex; gap: 6px; }
.reader-meta { display: flex; align-items: baseline; justify-content: space-between; padding: 12px 18px 6px; }
.reader-title { font-size: 16px; font-weight: 700; color: var(--text-1); }
.reader-pos { font-size: 12px; color: var(--text-3); display: inline-flex; align-items: center; gap: 6px; }
iframe { flex: 1; width: 100%; border: 0; background: #fff; }

@media (max-width: 1080px) {
  .search { width: 220px; }
  .manual-shell { grid-template-columns: 260px 1fr; }
}
@media (max-width: 860px) {
  .manual-shell { grid-template-columns: 220px 1fr; }
  .search { display: none; }
}
</style>
