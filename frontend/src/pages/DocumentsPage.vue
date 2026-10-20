<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Back,
  CopyDocument,
  Download,
  Edit,
  Folder,
  Refresh,
  Search,
  UploadFilled,
  View,
} from '@element-plus/icons-vue'
import type { UploadFile, UploadFiles, UploadRawFile } from 'element-plus'
import { marked } from 'marked'
import { api } from '@/api'
import { http as httpInstance } from '@/api'
import { sanitizeMarkdownHtml } from '@/utils/sanitize'
import { escapePlainText } from '@/utils/sanitize'
import DocxViewer from '@/components/DocxViewer.vue'
import {
  ACCEPTED_EXTENSIONS,
  ACCEPT_ATTR,
  CATEGORIES,
  VENDORS,
} from '@/constants/documents'

const router = useRouter()

// ── state ────────────────────────────────────────────────────────────────────
const list = ref<Record<string, unknown>[]>([])
const hits = ref<Record<string, unknown>[]>([])
const q = ref('')
const DEFAULT_CATEGORY = CATEGORIES[0]
const meta = reactive({
  title: '',
  vendor: '' as string,
  category: DEFAULT_CATEGORY,
  version_tag: '',
  source: '',
})
const loading = ref(false)
const detail = ref<Record<string, unknown> | null>(null)
const tab = ref<'list' | 'search' | 'upload' | 'detail'>('list')

// Filters for the document table
const filterVendor = ref<string>('')
const filterCategory = ref<string>('')

// Pagination state — drives refresh() and the el-pagination component.
// el-pagination uses 1-based pages; ``pageOffset`` is 0-based for the API.
const pageSize = ref(50)
const pageIndex = ref(1)
const totalDocs = ref(0)

const pageOffset = computed(() => (pageIndex.value - 1) * pageSize.value)

function onPageChange(p: number) {
  pageIndex.value = p
  refresh()
}

function onSizeChange(s: number) {
  pageSize.value = s
  pageIndex.value = 1
  refresh()
}

// Reset to page 1 whenever the user changes a filter — otherwise we end
// up requesting an offset past the end of a now-smaller filtered set.
watch([filterVendor, filterCategory], () => {
  pageIndex.value = 1
  refresh()
})

// Picked-but-not-uploaded file
const pendingFile = ref<File | null>(null)
const uploading = ref(false)

// Upload progress dialog
const uploadDialog = reactive({
  open: false,
  percent: 0,
  fileName: '',
  fileSize: 0,
})

// Edit dialog
const editDialog = reactive({
  open: false,
  id: 0 as number,
  title: '',
  vendor: '' as string,
  version_tag: '',
  source: '',
  saving: false,
})

const acceptAttr = ACCEPT_ATTR

/** Lower-case filename extension including the leading dot. */
const extOf = (name: string): string => {
  const i = name.lastIndexOf('.')
  return i < 0 ? '' : name.slice(i).toLowerCase()
}

const isAllowedExt = (name: string): boolean => {
  const lower = name.toLowerCase()
  if (lower.endsWith('.tar.gz')) return true
  return ACCEPTED_EXTENSIONS.includes(extOf(name))
}

const acceptHint = computed(
  () => `支持格式：${ACCEPTED_EXTENSIONS.length}+ 种（PDF/Word/Excel/华为 hwics/思科 bin/压缩包/配置脚本 等）`,
)

// ── list operations ──────────────────────────────────────────────────────────
async function exportData() {
  try {
    await api.exportData('documents', 'json')
    ElMessage.success('文档元数据已导出')
  } catch (err: unknown) {
    const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail ?? '导出失败')
  }
}

const refresh = async () => {
  loading.value = true
  try {
    // ``listDocuments`` now returns { items, total, limit, offset }; we keep
    // a flat ``list`` ref because the existing template iterates ``list``
    // directly. Pagination state lives in the dedicated refs below.
    const params: Record<string, string | number> = {
      limit: pageSize.value,
      offset: pageOffset.value,
    }
    if (filterVendor.value) params.vendor = filterVendor.value
    if (filterCategory.value) params.category = filterCategory.value
    const resp = await api.listDocuments(params)
    list.value = resp.items
    totalDocs.value = resp.total
  } finally {
    loading.value = false
  }
}

const search = async () => {
  if (!q.value.trim()) {
    hits.value = []
    return
  }
  const r = await api.searchDocuments(q.value)
  hits.value = (r as { hits: Record<string, unknown>[] }).hits
}

const open = async (id: number) => {
  // Jump to detail tab FIRST for instant UI feedback (no waiting on fetch).
  tab.value = 'detail'
  loading.value = true
  try {
    detail.value = (await api.getDocument(id)) as Record<string, unknown>
  } catch (err: unknown) {
    const msg = (err as { message?: string })?.message || '加载失败'
    ElMessage.error(msg)
    tab.value = 'list'
  } finally {
    loading.value = false
  }
}

const remove = async (id: number) => {
  await ElMessageBox.confirm('确认删除？此操作会同步删除 Obsidian 笔记。', '提示', { type: 'warning' })
  await api.deleteDocument(id)
  ElMessage.success('已删除')
  await refresh()
}

// ── edit ─────────────────────────────────────────────────────────────────────
const editRow = (row: Record<string, unknown>) => {
  editDialog.id = row.id as number
  editDialog.title = (row.title as string) || ''
  editDialog.vendor = (row.vendor as string) || ''
  editDialog.version_tag = (row.version_tag as string) || ''
  editDialog.source = (row.source as string) || ''
  editDialog.open = true
}

const submitEdit = async () => {
  if (!editDialog.title.trim()) {
    ElMessage.warning('标题不能为空')
    return
  }
  editDialog.saving = true
  try {
    await api.updateDocument(editDialog.id, {
      title: editDialog.title.trim(),
      vendor: editDialog.vendor.trim() || undefined,
      category: DEFAULT_CATEGORY,
      version_tag: editDialog.version_tag.trim() || undefined,
      source: editDialog.source.trim() || undefined,
    } as Record<string, unknown>)
    ElMessage.success('已更新（数据库 + Obsidian 同步）')
    editDialog.open = false
    await refresh()
  } catch (err: unknown) {
    const msg = (err as { response?: { data?: { detail?: string } }; message?: string })?.response?.data?.detail
      || (err as { message?: string })?.message
      || '未知错误'
    ElMessage.error(`更新失败：${msg}`)
  } finally {
    editDialog.saving = false
  }
}

// ── upload ───────────────────────────────────────────────────────────────────
const onFileChange = (file: UploadFile, _files: UploadFiles) => {
  const raw = (file.raw as UploadRawFile | undefined) ?? (file as unknown as File)
  pendingFile.value = raw ?? null
}

const onFileRemove = () => {
  pendingFile.value = null
}

const submitUpload = async () => {
  if (!pendingFile.value) {
    ElMessage.warning('请先选择文件')
    return
  }
  await upload(pendingFile.value)
}

const upload = async (file: File) => {
  if (!meta.title) { ElMessage.warning('请填写标题'); return }
  if (!meta.vendor) { ElMessage.warning('请选择或输入厂商'); return }
  if (!isAllowedExt(file.name)) {
    ElMessage.warning(`不支持的文件格式：${extOf(file.name) || '(无后缀)'}。${acceptHint.value}`)
    return
  }
  meta.category = DEFAULT_CATEGORY

  // Front-end soft cap: warn on huge files (server-side hard cap is 2 GB)
  const MAX_SOFT = 2 * 1024 * 1024 * 1024
  if (file.size > MAX_SOFT) {
    ElMessage.warning(`文件超过 2GB 上限（${fmtSize(file.size)}），请压缩或分卷`)
    return
  }
  if (file.size > 500 * 1024 * 1024) {
    try {
      await ElMessageBox.confirm(
        `文件较大（${fmtSize(file.size)}），上传可能需要数分钟，是否继续？`,
        '大文件上传确认',
        { confirmButtonText: '继续上传', cancelButtonText: '取消', type: 'warning' },
      )
    } catch { return } // user cancelled
  }

  // ── show progress dialog immediately for user feedback ──
  uploadDialog.open = true
  uploadDialog.percent = 0
  uploadDialog.fileName = file.name
  uploadDialog.fileSize = file.size

  try {
    uploading.value = true

    await api.uploadDocument(file, meta, (pct) => {
      uploadDialog.percent = pct
    })

    uploadDialog.percent = 100
    ElMessage.success(`「${meta.title}」上传成功`)
    pendingFile.value = null

    // Reset meta for next upload
    meta.title = ''
    meta.vendor = ''
    meta.version_tag = ''
    meta.source = ''

    // Belt-and-braces: even if the list refresh hangs, the dialog must
    // close and the tab must switch. The previous implementation only
    // reached this setTimeout when refresh() resolved — so a single
    // unrelated error there stranded users on a "准备跳转列表…" dialog.
    let refreshed = false
    try {
      await refresh()
      refreshed = true
    } catch (refreshErr) {
      // refresh failed for unrelated reasons (e.g. transient 500 on
      // list endpoint); surface the warning but never block the jump.
      // eslint-disable-next-line no-console
      console.warn('refresh after upload failed:', refreshErr)
    }

    const closeAndJump = () => {
      uploadDialog.open = false
      tab.value = 'list'
    }
    setTimeout(closeAndJump, refreshed ? 600 : 1200)
    // Hard fallback — if the dialog is somehow never closed (animation
    // stuck / z-index issue / Element Plus quirk on slow machines), this
    // guarantees the user is not stranded.
    setTimeout(closeAndJump, 4000)
  } catch (err: unknown) {
    const msg = (err as { response?: { data?: { detail?: string } }; message?: string })?.response?.data?.detail
      || (err as { message?: string })?.message
      || '未知错误'
    ElMessage.error(`上传失败：${msg}`)
    uploadDialog.open = false
  } finally {
    uploading.value = false
  }
}

// ── formatting helpers ───────────────────────────────────────────────────────
const fmtSize = (bytes: number) => {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

const fmtDate = (s: unknown) => {
  if (!s) return '—'
  const d = new Date(s as string)
  return isNaN(d.getTime()) ? String(s) : d.toLocaleString('zh-CN')
}

// ── detail: TOC + section rendering (Huawei-style manual page) ───────────────

// One section in the table of contents — either a flat `## filename` heading
// or a folder-style entry (path components grouped together).
interface TocItem {
  id: string          // anchor id used in URL hash + DOM
  title: string       // display text
  subtitle?: string   // secondary text (folder path for grouped items)
  level: number       // 2 = `##`, 3 = `###`
  isFolder?: boolean  // true = folder header in the sidebar
}

// Slugify a heading so we can use it as a DOM id and URL hash.
const slugify = (s: string): string => {
  const base = String(s || '')
    .replace(/[`*_~\[\]()#]/g, '')
    .replace(/\s+/g, '-')
    .replace(/[^一-龥\w\-\.\/]/g, '')
    .toLowerCase()
  return base || 'section'
}

// Parse the raw content into a list of sections. Each `## heading` starts a
// new section, and everything until the next `## ` belongs to it. If the
// content has no `## ` at all, we fall back to a single "正文" section.
//
// Infrastructure files (profile.xml, navi.xml, hedex-homepage.html, etc.) are
// kept around as a "metadata" group so the sidebar doesn't drown the actual
// document topics.
interface Section {
  id: string
  title: string           // display name (file basename or cleaned-up title)
  rawTitle: string        // original heading text from `## `
  body: string            // raw markdown body for this section
  group: string | null    // folder part of the path (e.g. "topics/dkx")
  isMeta: boolean         // true for profile/navi/hedex files
}

const isInfrastructureFile = (rawTitle: string): boolean => {
  const lower = rawTitle.toLowerCase()
  // Common Huawei HDX structural files — not part of the document body.
  if (lower === 'profile.xml') return true
  if (lower.endsWith('/profile.xml')) return true
  if (lower.endsWith('navi.xml')) return true
  if (lower.endsWith('hedex-homepage.html') || lower.endsWith('homepage.html')) return true
  if (lower.endsWith('/hedex-homepage.html')) return true
  if (lower.endsWith('.css') || lower.endsWith('.js')) return true
  return false
}

const cleanTitle = (raw: string): { title: string; group: string | null; fileExt: string } => {
  const lower = raw.toLowerCase()
  let group: string | null = null
  let display = raw

  // Path-style heading:  resources/foo/bar.html  →  group=resources/foo, title=bar.html
  const slashIdx = raw.lastIndexOf('/')
  if (slashIdx > 0) {
    group = raw.slice(0, slashIdx)
    display = raw.slice(slashIdx + 1)
  }

  // Strip file extension for display when there's a meaningful extension.
  let fileExt = ''
  const dotIdx = display.lastIndexOf('.')
  if (dotIdx > 0 && dotIdx > display.length - 8) {
    fileExt = display.slice(dotIdx + 1).toLowerCase()
    if (['html', 'htm', 'xhtml', 'xml', 'txt', 'md', 'markdown'].includes(fileExt)) {
      display = display.slice(0, dotIdx)
    }
  }

  // Replace _ and - separators with spaces, then collapse repeats.
  display = display
    .replace(/[_\-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()

  return { title: display || raw, group, fileExt }
}

const parseSections = (raw: string): { intro: string; sections: Section[] } => {
  if (!raw) return { intro: '', sections: [] }
  const lines = raw.split(/\r?\n/)
  const sections: Section[] = []
  let intro: string[] = []
  let current: Section | null = null

  const flush = () => {
    if (current) {
      current.body = current.body.trim()
      if (current.body || !current.isMeta) sections.push(current)
    }
  }

  for (const line of lines) {
    const m = line.match(/^##\s+(.+?)\s*$/)
    if (m) {
      flush()
      const rawTitle = m[1].trim()
      const id = `s-${slugify(rawTitle)}-${sections.length + 1}`
      const { title, group } = cleanTitle(rawTitle)
      current = {
        id,
        title,
        rawTitle,
        group,
        body: '',
        isMeta: isInfrastructureFile(rawTitle),
      }
    } else if (current) {
      current.body += line + '\n'
    } else {
      intro.push(line)
    }
  }
  flush()
  return { intro: intro.join('\n').trim(), sections }
}

const parsed = computed(() => {
  const c = (detail.value as { content?: string } | null)?.content || ''
  return parseSections(c)
})

const visibleSections = computed(() =>
  parsed.value.sections.filter((s) => !s.isMeta),
)

const metaSections = computed(() =>
  parsed.value.sections.filter((s) => s.isMeta),
)

const introHtml = computed(() => {
  if (!parsed.value.intro) return ''
  return sanitizeMarkdownHtml(marked.parse(parsed.value.intro, { async: false }) as string)
})

const renderMarkdown = (body: string) =>
  sanitizeMarkdownHtml(marked.parse(body, { async: false, breaks: false }) as string)

// TOC: tree of visible sections, grouped by their `group` (folder path).
// Returns a flat list suitable for the sidebar — folders come first,
// then their child entries indented.
interface FlatTocNode {
  id: string | null         // null = folder header (not scrollable)
  title: string
  subtitle?: string
  isFolder: boolean
  level: number
}

const tocTree = computed<FlatTocNode[]>(() => {
  const groups = new Map<string, Section[]>()
  for (const s of visibleSections.value) {
    const key = s.group || ''
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key)!.push(s)
  }

  // Stable folder order — sort by first appearance in content.
  const orderedKeys: string[] = []
  const seen = new Set<string>()
  for (const s of visibleSections.value) {
    const key = s.group || ''
    if (!seen.has(key)) {
      seen.add(key)
      orderedKeys.push(key)
    }
  }

  const nodes: FlatTocNode[] = []
  for (const key of orderedKeys) {
    const items = groups.get(key)!
    if (key) {
      nodes.push({ id: null, title: key, isFolder: true, level: 1 })
    }
    for (const s of items) {
      nodes.push({
        id: s.id,
        title: s.title,
        subtitle: s.group || '',
        isFolder: false,
        level: 2,
      })
    }
  }
  return nodes
})

const tocItems = computed(() =>
  tocTree.value.filter((n) => !n.isFolder && (!tocFilter.value ||
    n.title.toLowerCase().includes(tocFilter.value.toLowerCase()) ||
    (n.subtitle || '').toLowerCase().includes(tocFilter.value.toLowerCase()))),
)

// Scroll-spy — track which section is currently in view.
const activeSectionId = ref<string>('')
const detailScrollRef = ref<HTMLElement | null>(null)

const scrollToSection = (id: string) => {
  const el = document.getElementById(id)
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'start' })
    activeSectionId.value = id
  }
}

// Reset scroll + active section whenever the document changes.
watch(
  () => (detail.value as { id?: number } | null)?.id,
  async () => {
    activeSectionId.value = ''
    await nextTick()
    if (detailScrollRef.value) detailScrollRef.value.scrollTop = 0
  },
)

// TOC filter (sidebar search box)
const tocFilter = ref('')

// Scroll back to top button.
const showScrollTop = ref(false)
const onContentScroll = (e: Event) => {
  const root = e.target as HTMLElement
  showScrollTop.value = root.scrollTop > 400
  // Find the section whose top is closest to (but not above) the scroll top + offset.
  const offset = 80
  let current = parsed.value.sections[0]?.id || ''
  for (const s of parsed.value.sections) {
    const el = document.getElementById(s.id)
    if (!el) continue
    const top = el.offsetTop - root.offsetTop
    if (top - offset <= root.scrollTop) current = s.id
  }
  activeSectionId.value = current
}
const scrollToTop = () => {
  if (detailScrollRef.value) detailScrollRef.value.scrollTo({ top: 0, behavior: 'smooth' })
}

// Breadcrumb navigation — vendor/category crumbs jump to the list with a
// pre-applied filter so the user lands on the matching subset of docs.
const goToList = () => {
  filterVendor.value = ''
  filterCategory.value = ''
  tab.value = 'list'
}
const filterByCategory = (category?: string) => {
  filterCategory.value = category || ''
  filterVendor.value = ''
  tab.value = 'list'
}
const filterByVendor = (vendor?: string) => {
  filterVendor.value = vendor || ''
  tab.value = 'list'
}
const nodeMatchesFilter = (n: { title: string; subtitle?: string }) => {
  const q = tocFilter.value.toLowerCase()
  if (!q) return true
  return n.title.toLowerCase().includes(q) || (n.subtitle || '').toLowerCase().includes(q)
}

const copyCodeBlocks = async () => {
  try {
    await navigator.clipboard.writeText((detail.value as { content?: string } | null)?.content || '')
    ElMessage.success('内容已复制到剪贴板')
  } catch {
    ElMessage.error('复制失败：浏览器拒绝访问剪贴板')
  }
}

const detailFileExt = computed(() => {
  const fn = ((detail.value as { filename?: string })?.filename || '').toLowerCase()
  const i = fn.lastIndexOf('.')
  return i < 0 ? '' : fn.slice(i + 1)
})

const PREVIEWABLE_EXTS = new Set([
  'docx', 'pptx', 'xlsx', 'csv',
  'pdf', 'md', 'markdown', 'mdown',
  'html', 'htm',
  'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp', 'svg',
])

function isPreviewableExt(ext: string): boolean {
  if (!ext) return false
  // mammoth doesn't handle legacy .doc — still expose it through the
  // viewer so the user sees an honest notice instead of the garbled
  // blob-the-backend-returned rendering.
  return PREVIEWABLE_EXTS.has(ext) || ext === 'doc'
}

interface DetailHealth {
  score: number
  grade: 'ok' | 'minor' | 'warn' | 'broken'
  references_total: number
  references_missing: number
  references_case_only: number
  missing_targets: Array<[string, number]>
  case_only_targets: Array<[string, number]>
}

const detailHealth = computed<DetailHealth | null>(
  () => (detail.value as unknown as { offline_health?: DetailHealth | null })?.offline_health ?? null,
)

function healthTagType(grade: DetailHealth['grade']): 'success' | 'info' | 'warning' | 'danger' {
  switch (grade) {
    case 'ok': return 'success'
    case 'minor': return 'info'
    case 'warn': return 'warning'
    default: return 'danger'
  }
}

const isPdfPreviewable = computed(
  () => !((detail.value as { is_binary?: boolean })?.is_binary) ||
        detailFileExt.value === 'pdf',
)

// ── CHM-specific helpers ──────────────────────────────────────────────────────
// CHM is Microsoft Compiled HTML Help (ITSF/ITSS compressed).  No
// Python CHM reader ships in this project's dependency set, so the
// platform can't render CHM content directly.  Instead the card
// gives the operator a one-click jump to the *vendor's* official
// online product documentation — the same content the CHM bundles,
// served by the vendor themselves — and lets them open the local
// CHM file as a fallback.
const isChmExt = computed(() => detailFileExt.value === 'chm')

interface ParsedProduct {
  /** Best-effort product family identifier (e.g. "AR600", "S5720",
   * "Catalyst 9300"). Empty string when nothing matches. */
  family: string
  /** Best-effort software / hardware version (e.g. "V300R024",
   * "V2", "15.7"). Empty string when nothing matches. */
  version: string
}

/** Parse a product name + version out of a CHM filename / doc title.
 *
 * We deliberately try the *filename* first because that carries the
 * original vendor label (e.g. "NetEngine AR600, AR6100, AR6200,
 * AR6300 V300R024 产品文档.chm"), and only fall back to the upload
 * ``title`` if filename doesn't yield anything — operators sometimes
 * rename the title in the metadata dialog and we'd lose the vendor
 * cue.
 *
 * Patterns covered:
 *   - Huawei NetEngine / AR / S / CE / Agile / NE series + VxxxRxxx
 *   - Huawei OptiX / NExxxx + Vxxx
 *   - H3C S / MS / SecBlade / F / CAS + version (V*)
 *   - Cisco Catalyst / ISR / ASR / Nexus + version (15.x / 17.x)
 *   - Ruijie RG / S / NBR + version
 *
 * Anything that doesn't match returns ``{family: '', version: ''}``
 * and the caller falls back to a vendor-only search URL. */
function parseProductKey(filename: string, title: string): ParsedProduct {
  const sources = [filename, title].map((s) => (s || '').toLowerCase())
  const family_re = [
    // Huawei NetEngine AR / S / CE / Agile — multi-digit series.
    /(netengine\s*)?\b(?:ar|s|ce|agile)[a-z]*[-\s]?(\d{3,4}[a-z]?)\b/i,
    // Huawei OptiX / NE / CX — alphanumeric, e.g. NE40E, CX600.
    /\b(optix|ne|cx)\s?(\d{2,4}[a-z]*)\b/i,
    // H3C S / MS / SecBlade / F series — S6850, MS4520, etc.
    /\b(s|ms|secblade|f|cas|rx)\s?(\d{3,4}[a-z]?)\b/i,
    // Cisco Catalyst / ISR / ASR / Nexus — 9000, 4331, 9200, etc.
    /\b(catalyst|isr|asr|nexus)\s?(\d{3,4}[a-z]?)\b/i,
    // Ruijie RG / S / NBR.
    /\b(rg|s|nbr)\s?(\d{3,4}[a-z]?)\b/i,
  ]
  const version_re = [
    /\bv(\d{3,4})\s?r?(\d{2,3})\b/i,         // Huawei V300R024
    /\bv(\d+(?:\.\d+)+)\b/i,                  // Huawei V2.10 / generic V1.0
    /\bv(\d{1,2})\b/i,                          // Generic V2 / V3
    /\b(\d{2,3}\.\d+(?:\.\d+)?)\b/i,        // Cisco 15.7 / 17.3.1
  ]
  let family = ''
  let version = ''
  for (const src of sources) {
    if (!family) {
      for (const re of family_re) {
        const m = src.match(re)
        if (m) { family = m[0].trim(); break }
      }
    }
    if (!version) {
      for (const re of version_re) {
        const m = src.match(re)
        if (m) { version = m[0].trim(); break }
      }
    }
    if (family && version) break
  }
  // Tidy: strip the .chm extension and trim punctuation.
  family = family.replace(/\.chm$/i, '').trim()
  version = version.trim()
  return { family, version }
}

/** Vendor-aware official documentation URL builder.
 *
 * Each vendor gets a primary URL pattern that targets their product
 * doc index; the platform also keeps an enterprise-support search
 * URL as a fallback when no specific product page is known.  The
 * search URL is the *always works* option because it just lists
 * matches for whatever keyword we pass.
 *
 * Vendor keys are matched case-insensitively against both the
 * document's ``vendor`` field and the parsed family token, so an
 * operator who types "Huawei" or "huawei" or even "HW" gets routed
 * to the Huawei Enterprise KB. */
const _VENDOR_URL_BUILDERS: Array<{
  match: RegExp
  product: (p: ParsedProduct) => string | null
  search: (kw: string) => string
}> = [
  {
    // Huawei Enterprise — primary: enterprise product support portal.
    match: /huawei|hw\b/i,
    product: (p) => {
      const kw = [p.family, p.version].filter(Boolean).join(' ')
      return kw ? `https://support.huawei.com/enterprise/zh/search?keyword=${encodeURIComponent(kw)}` : null
    },
    search: (kw) => `https://support.huawei.com/enterprise/zh/search?keyword=${encodeURIComponent(kw)}`,
  },
  {
    // H3C — official magicbox KB has a search-by-keyword page.
    match: /h3c|新华三/i,
    product: (p) => {
      const kw = [p.family, p.version].filter(Boolean).join(' ')
      return kw ? `https://www.h3c.com/cn/Service/Document_Search/Document/${encodeURIComponent(kw)}/` : null
    },
    search: (kw) => `https://www.h3c.com/cn/Service/Document_Search/?keyword=${encodeURIComponent(kw)}`,
  },
  {
    // Cisco — product docs are served from the unified support portal.
    match: /cisco|思科/i,
    product: (p) => {
      const kw = [p.family, p.version].filter(Boolean).join(' ')
      return kw ? `https://www.cisco.com/c/en/us/support/search.html?q=${encodeURIComponent(kw)}` : null
    },
    search: (kw) => `https://www.cisco.com/c/en/us/support/search.html?q=${encodeURIComponent(kw)}`,
  },
  {
    // Ruijie — official RCN has a search-by-keyword endpoint.
    match: /ruijie|锐捷/i,
    product: (p) => {
      const kw = [p.family, p.version].filter(Boolean).join(' ')
      return kw ? `https://www.ruijie.com.cn/page/3/?keyword=${encodeURIComponent(kw)}` : null
    },
    search: (kw) => `https://www.ruijie.com.cn/page/3/?keyword=${encodeURIComponent(kw)}`,
  },
  {
    // Generic fallback — Huawei search handles every vendor well
    // enough when nothing else matches (returns the operator's
    // keyword as a Google-style hit list).
    match: /.*/,
    product: () => null,
    search: (kw) => `https://support.huawei.com/enterprise/zh/search?keyword=${encodeURIComponent(kw)}`,
  },
]

function officialDocLink(): { url: string; vendor: string; label: string } {
  const filename = (detail.value as { filename?: string })?.filename || ''
  const title = (detail.value as { title?: string })?.title || ''
  const vendor = (detail.value as { vendor?: string })?.vendor || ''
  const product = parseProductKey(filename, title)
  const vendorKey = (vendor || product.family).trim()
  const builder = _VENDOR_URL_BUILDERS.find((b) => b.match.test(vendorKey)) || _VENDOR_URL_BUILDERS[_VENDOR_URL_BUILDERS.length - 1]
  const keyword = [product.family, product.version].filter(Boolean).join(' ').trim() || title || filename.replace(/\.chm$/i, '')
  const url = builder.product(product) || builder.search(keyword)
  return {
    url,
    vendor: vendor || '厂商',
    label: product.family
      ? `打开《${product.family}${product.version ? ' ' + product.version : ''}》在线文档`
      : `打开${vendor || '该厂商'}在线文档`,
  }
}

function openOfficialDoc() {
  const { url, label } = officialDocLink()
  window.open(url, '_blank', 'noopener,noreferrer')
  ElMessage.success(`已在新标签页打开：${label}`)
}

/** Detect the user's OS so we can decide whether the local-CHM
 *  shortcut is even meaningful.  ``hh.exe`` ships with Windows
 *  since NT 4.0 and is the only OS-resident CHM handler in the
 *  wild — macOS / Linux users have to install something (xCHM,
 *  KCHMViewer) and we'd just be guessing. */
const isWindows = computed(() => /windows/i.test(navigator.userAgent || ''))

/** Track which documents already have the ``naw-chm://`` protocol
 *  registered on the user's machine.  The registration is per-doc
 *  (each .reg embeds the absolute CHM path) so the set is stored
 *  keyed by ``document.id`` in localStorage. */
function chmRegKey(id: number) { return `naw-chm-registered:${id}` }

function isChmHandlerRegistered(id: number): boolean {
  try {
    return localStorage.getItem(chmRegKey(id)) === '1'
  } catch {
    return false
  }
}

function markChmHandlerRegistered(id: number): void {
  try {
    localStorage.setItem(chmRegKey(id), '1')
  } catch {
    /* ignore — if storage is blocked the operator can still register
     * on next visit; the worst case is they re-download the .reg */
  }
}

/** Reset the "registered" flag for a document.  Surfaced so the
 *  operator can recover if they cleared the registry but not the
 *  localStorage entry (or vice versa). */
function forgetChmHandlerRegistration(id: number): void {
  try { localStorage.removeItem(chmRegKey(id)) } catch { /* ignore */ }
}

/** Open the local CHM file with the system's default handler
 *  (i.e. the same thing double-clicking it in Explorer does).
 *
 *  Two flows depending on whether the operator has already merged
 *  the protocol-handler .reg for this document:
 *
 *  1. **First time** — no localStorage entry for this doc.  We
 *     prompt them to download a tiny ``.reg`` file (one-click
 *     Windows registry merge) which registers the
 *     ``naw-chm://open?id=<n>`` URL scheme.  After they confirm
 *     the merge in the UAC prompt and the browser does its
 *     "external protocol" handshake, the OS will route future
 *     ``naw-chm://`` links to ``hh.exe <absolute-path>``.  This is
 *     a **per-machine, per-document** registration — different
 *     CHM files have different registry entries pointing at their
 *     respective paths.
 *
 *  2. **Subsequent times** — localStorage flag is set.  We
 *     navigate the browser to ``naw-chm://open?id=<n>``.  Edge /
 *     Chrome pop up an "open this external protocol?" prompt once,
 *     then the OS opens hh.exe with the document.  This is what the
 *     PowerShell clipboard dance was trying to emulate, but without
 *     leaving the browser.
 *
 *  Why we don't just hard-link ``naw-chm://`` to ``hh.exe <url>``
 *  in the .reg:  hh.exe accepts a CHM *path*, not a URL, so we have
 *  to resolve the doc-id to the absolute path *somewhere*.  Doing
 *  it server-side keeps the registry file under a kilobyte and
 *  avoids the operator editing it by hand. */
async function openLocalChm() {
  if (!isWindows.value) {
    ElMessage.warning('本机非 Windows，hh.exe 协议注册不可用。请使用 macOS / Linux 的 CHM 阅读器（xCHM、KCHMViewer）打开下载的文件。')
    return
  }
  const id = (detail.value as { id?: number })?.id
  if (!id) {
    ElMessage.warning('该文档没有可识别的 ID')
    return
  }
  const filename = (detail.value as { filename?: string })?.filename || `doc-${id}`
  // Already registered?  Just navigate to the protocol URL.
  if (isChmHandlerRegistered(id)) {
    try {
      window.location.href = `naw-chm://open?id=${id}`
      return
    } catch (err) {
      // fall through to the registration flow on failure
      console.warn('naw-chm:// navigation failed', err)
    }
  }
  // First-time flow: walk the operator through the .reg download.
  // We keep the original PowerShell fallback for environments where
  // they don't want to touch the registry (locked-down corporate
  // machines, antivirus blocking, etc.).
  const cmd = `Start-Process hh.exe "${filename.replace(/"/g, '\\"')}"`
  ElMessageBox.alert(
    [
      `弹出本地 hh.exe 阅读器（一次性注册 + 之后一键弹出）：`,
      ``,
      `① 点击下方【下载注册文件】，浏览器会下载一个不到 1KB 的 .reg 文件；`,
      `② 双击下载的 .reg 文件，Windows 弹出 "是否继续" 确认 → 点【是】；`,
      `   （注册表只写到当前用户 HKCU，不需要管理员权限）`,
      `③ 回到本页面，再次点击【用本地程序打开】，浏览器询问"是否打开外部协议"时点【打开】；`,
      `   系统自动用 hh.exe 弹出 CHM 阅读器，效果与双击 CHM 文件完全相同。`,
      ``,
      `如果不想改注册表，仍可使用 PowerShell 命令（Win+R → 输入 powershell → 回车 → 粘贴执行）：`,
      `    ${cmd}`,
    ].join('\n'),
    '用本地程序打开 CHM',
    {
      confirmButtonText: '下载注册文件 (.reg)',
      cancelButtonText: '我知道了',
      showCancelButton: true,
      distinguishCancelAndClose: true,
    },
  ).then(async () => {
    await downloadChmHandlerReg(id)
    markChmHandlerRegistered(id)
  }).catch(() => {
    /* user closed the dialog without confirming — no-op */
  })
}

/** Download the protocol-handler .reg for a given document id. */
async function downloadChmHandlerReg(id: number) {
  try {
    const resp = await httpInstance.get(`/offline-manual/${id}/chm-handler.reg`, { responseType: 'blob' })
    const blob = new Blob([resp.data as BlobPart], {
      type: 'application/x-registry-edit',
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `naw-chm-handler-${id}.reg`
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    ElMessage.success({
      message: '注册文件已下载 — 双击它即可完成 .reg 注册（HKCU 不需要管理员）',
      duration: 6000,
    })
  } catch (err: unknown) {
    const msg = (err as { message?: string })?.message || '下载注册文件失败'
    ElMessage.error(msg)
  }
}

/** Forget the per-document "handler registered" flag.  Called when
 *  the operator wants to re-trigger the registration flow (e.g. they
 *  cleared the registry by hand, or the file moved). */
function resetChmHandlerRegistration(id: number) {
  forgetChmHandlerRegistration(id)
  ElMessage.info('已重置 — 下次点击会用本地程序打开时，会重新提示注册流程')
}

async function copyAiPrompt() {
  const title = (detail.value as { title?: string })?.title || ''
  const vendor = (detail.value as { vendor?: string })?.vendor || ''
  const summary = (detail.value as { summary?: string })?.summary || ''
  const { url } = officialDocLink()
  const lines = [
    vendor ? `请介绍《${title}》（${vendor} 厂商）这份手册覆盖哪些内容。` : `请介绍《${title}》这份手册覆盖哪些内容。`,
    summary ? `（已知摘要：${summary}）` : '',
    '本地有 CHM 格式副本（无法直接预览）。请帮我：',
    '1. 总结这本手册通常包含哪些章节/主题；',
    '2. 我应该重点查阅哪几节来解决我接下来要问的问题。',
    `3. 官方在线文档入口：${url}`,
  ].filter(Boolean)
  const prompt = lines.join('\n')
  try {
    await navigator.clipboard.writeText(prompt)
    ElMessage.success('已复制「AI 询问模板」，请到 AI 助手粘贴发送')
  } catch {
    ElMessage.warning('剪贴板不可用，请手动复制下方模板')
  }
}

// ── filters + download ────────────────────────────────────────────────────────
// Distinct vendor values from current list, for the filter dropdown.
const availableVendors = computed(() => {
  const set = new Set<string>()
  for (const row of list.value) {
    const v = (row as { vendor?: string }).vendor
    if (v) set.add(v)
  }
  return [...set].sort()
})

const availableCategories = computed(() => {
  const set = new Set<string>()
  for (const row of list.value) {
    const c = (row as { category?: string }).category
    if (c) set.add(c)
  }
  return [...set].sort()
})

const filteredList = computed(() => {
  return list.value.filter((row) => {
    if (filterVendor.value && (row as { vendor?: string }).vendor !== filterVendor.value) return false
    if (filterCategory.value && (row as { category?: string }).category !== filterCategory.value) return false
    return true
  })
})

const resetFilters = () => {
  filterVendor.value = ''
  filterCategory.value = ''
}

const download = async (row: Record<string, unknown>) => {
  const id = (row as { id: number }).id
  const filename = (row as { filename: string }).filename || `doc-${id}`
  try {
    const resp = await httpInstance.get(`/documents/${id}/download`, { responseType: 'blob' })
    const blob = new Blob([resp.data as BlobPart])
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    ElMessage.success(`已开始下载：${filename}`)
  } catch (err: unknown) {
    const msg = (err as { message?: string })?.message || '下载失败'
    ElMessage.error(msg)
  }
}

onMounted(refresh)
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="page-header">
      <span class="page-title">手册库</span>
      <div class="header-right">
        <el-input v-model="q" placeholder="搜索关键字" clearable style="width:260px" @keyup.enter="search" @clear="hits=[]">
          <template #append><el-button type="primary" @click="search">搜索</el-button></template>
        </el-input>
        <el-button :icon="Refresh" @click="refresh">刷新</el-button>
        <el-button type="primary" :icon="UploadFilled" @click="tab='upload'">上传</el-button>
        <el-button :icon="Download" @click="exportData">导出</el-button>
      </div>
    </div>

    <div class="tabs-card">
      <el-tabs v-model="tab">
        <!-- ── list ── -->
        <el-tab-pane label="全部文档" name="list">
          <el-table :data="list" border stripe size="small">
            <el-table-column type="index" label="#" width="50" align="center" />
            <el-table-column prop="title" label="标题" min-width="200" show-overflow-tooltip />
            <el-table-column prop="vendor" label="厂商" width="130" />
            <el-table-column prop="category" label="分类" width="130" show-overflow-tooltip />
            <el-table-column prop="version_tag" label="版本" width="110" />
            <el-table-column label="大小" width="100" align="right">
              <template #default="{ row }">
                {{ fmtSize((row as {size_bytes:number}).size_bytes) }}
              </template>
            </el-table-column>
            <el-table-column label="上传时间" min-width="160">
              <template #default="{ row }">
                {{ fmtDate((row as {created_at:string}).created_at) }}
              </template>
            </el-table-column>
            <el-table-column prop="uploaded_by" label="上传人" width="100" />
            <el-table-column label="操作" width="280" align="center" fixed="right">
              <template #default="{ row }">
                <el-button size="small" link type="primary" :icon="View" @click="open((row as {id:number}).id)">详情</el-button>
                <el-button size="small" link type="success" :icon="Download" @click="download(row)">下载</el-button>
                <el-button size="small" link type="warning" :icon="Edit" @click="editRow(row)">改元数据</el-button>
                <el-button size="small" link type="danger" @click="remove((row as {id:number}).id)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div class="pager-row">
            <el-pagination
              v-model:current-page="pageIndex"
              v-model:page-size="pageSize"
              :total="totalDocs"
              :page-sizes="[20, 50, 100, 200]"
              layout="total, sizes, prev, pager, next, jumper"
              background
              small
              @current-change="onPageChange"
              @size-change="onSizeChange"
            />
          </div>
        </el-tab-pane>

        <!-- ── search results ── -->
        <el-tab-pane label="搜索结果" name="search">
          <el-empty v-if="!hits.length" description="暂无结果" :image-size="60" />
          <div v-for="h in hits" :key="(h as {id:number}).id" class="hit-card" @click="open((h as {id:number}).id)">
            <div class="hit-title">{{ h.title }}</div>
            <div class="hit-meta">{{ h.vendor }} · {{ h.category }}</div>
            <pre class="hit-snippet">{{ h.snippet }}</pre>
          </div>
        </el-tab-pane>

        <!-- ── upload form ── -->
        <el-tab-pane label="上传文档" name="upload">
          <el-form label-width="90px" style="max-width:640px">
            <el-form-item label="标题" required>
              <el-input v-model="meta.title" placeholder="例：S5700 配置指南" />
            </el-form-item>
            <el-form-item label="厂商" required>
              <el-select
                v-model="meta.vendor"
                filterable
                allow-create
                default-first-option
                clearable
                placeholder="选择或输入厂商"
                style="width:100%"
              >
                <el-option v-for="v in VENDORS" :key="v" :label="v" :value="v" />
              </el-select>
            </el-form-item>
            <el-form-item label="分类">
              <el-input :model-value="meta.category" readonly disabled />
            </el-form-item>
            <el-form-item label="版本">
              <el-input v-model="meta.version_tag" placeholder="例：V800R024C00" />
            </el-form-item>
            <el-form-item label="来源">
              <el-input v-model="meta.source" placeholder="例：官方下载 / 内部整理" />
            </el-form-item>
            <el-form-item label="文件">
              <el-upload
                :auto-upload="false"
                :show-file-list="false"
                :accept="acceptAttr"
                :on-change="onFileChange"
                :on-remove="onFileRemove"
                drag
              >
                <div class="upload-hint">
                  <el-icon class="upload-icon"><upload-filled /></el-icon>
                  <div v-if="!pendingFile">点击或拖拽文件到这里选择</div>
                  <div v-else class="pending-info">
                    已选择：<b>{{ pendingFile.name }}</b>
                    <span class="pending-size">（{{ fmtSize(pendingFile.size) }}）</span>
                  </div>
                  <div class="upload-sub">{{ acceptHint }}</div>
                </div>
              </el-upload>
            </el-form-item>
            <el-form-item>
              <el-button
                type="primary"
                :loading="uploading"
                :disabled="!pendingFile"
                @click="submitUpload"
              >
                {{ uploading ? '上传中…' : '开始上传' }}
              </el-button>
              <el-button v-if="pendingFile" @click="onFileRemove">清除选择</el-button>
            </el-form-item>
          </el-form>
        </el-tab-pane>

        <!-- ── detail (Huawei manual style) ── -->
        <el-tab-pane label="文档详情" name="detail">
          <div v-if="detail" class="manual-layout">
            <!-- Top breadcrumb / toolbar -->
            <div class="manual-header">
              <div class="manual-crumbs">
                <span class="crumb crumb-link" @click="goToList()">
                  <el-icon><Back /></el-icon>
                  <span style="margin-left:2px">手册库</span>
                </span>
                <span class="crumb-sep">/</span>
                <span class="crumb crumb-link" @click="filterByCategory(detail.category)">
                  {{ detail.category || '产品手册' }}
                </span>
                <template v-if="detail.vendor">
                  <span class="crumb-sep">/</span>
                  <span class="crumb crumb-link" @click="filterByVendor(detail.vendor)">
                    {{ detail.vendor }}
                  </span>
                </template>
                <span class="crumb-sep">/</span>
                <span class="crumb crumb-current" :title="detail.title">{{ detail.title }}</span>
              </div>
              <div class="manual-toolbar">
                <el-tag size="small" type="info">{{ detail.vendor || '未指定厂商' }}</el-tag>
                <el-tag size="small" effect="plain">{{ detail.category || '产品手册' }}</el-tag>
                <el-tag size="small" v-if="detail.version_tag" effect="plain">v{{ detail.version_tag }}</el-tag>
                <el-tag
                  v-if="detailHealth"
                  :type="healthTagType(detailHealth.grade)"
                  size="small"
                  effect="dark"
                  class="health-pill"
                >
                  包体健康度 {{ detailHealth.score }} 分
                </el-tag>
                <span class="toolbar-spacer" />
                <el-button :icon="CopyDocument" size="small" plain @click="copyCodeBlocks" v-if="!((detail as {is_binary?:boolean}).is_binary) && visibleSections.length">复制内容</el-button>
                <el-button v-if="detailFileExt === 'hdx'" size="small" type="success" plain @click="router.push(`/documents/${(detail as {id:number}).id}/offline`)">原生离线手册</el-button>
                <el-button v-if="isChmExt" size="small" type="success" @click="openOfficialDoc">打开官方在线文档</el-button>
                <el-button v-if="isChmExt && isWindows" size="small" type="warning" plain @click="openLocalChm">
                  {{ isChmHandlerRegistered((detail as { id: number }).id) ? '在本机打开' : '用本地程序打开' }}
                </el-button>
                <el-button :icon="Download" size="small" type="primary" @click="download(detail)">下载原文件</el-button>
                <el-button :icon="Edit" size="small" type="warning" plain @click="editRow(detail)">改元数据</el-button>
              </div>
            </div>

            <!-- Main area: TOC sidebar + content pane -->
            <div class="manual-body">
              <!-- ── TOC sidebar ── -->
              <aside class="manual-toc" v-if="!((detail as {is_binary?:boolean}).is_binary) && tocTree.length">
                <div class="toc-title">
                  <el-icon><Folder /></el-icon>
                  <span>本文目录</span>
                  <span class="toc-count">{{ visibleSections.length }}</span>
                </div>
                <div class="toc-search">
                  <el-input
                    v-model="tocFilter"
                    placeholder="筛选章节…"
                    size="small"
                    clearable
                  >
                    <template #prefix><el-icon><Search /></el-icon></template>
                  </el-input>
                </div>
                <ul class="toc-list">
                  <template v-for="(node, idx) in tocTree" :key="node.id || `folder-${idx}-${node.title}`">
                    <!-- Folder header -->
                    <li v-if="node.isFolder" class="toc-folder">{{ node.title }}</li>
                    <!-- Section link -->
                    <li
                      v-else-if="!tocFilter || nodeMatchesFilter(node)"
                      class="toc-item"
                      :class="{ active: activeSectionId === node.id }"
                      @click="scrollToSection(node.id!)"
                    >
                      <div class="toc-item-title">{{ node.title }}</div>
                      <div class="toc-item-sub" v-if="node.subtitle">{{ node.subtitle }}</div>
                    </li>
                  </template>
                </ul>
              </aside>

              <!-- ── Content pane ── -->
              <main class="manual-content" ref="detailScrollRef" @scroll="onContentScroll">
                <!-- Document meta card -->
                <div class="manual-meta-card">
                  <div class="meta-card-grid">
                    <div><span>版本</span><strong>{{ detail.version_tag || '—' }}</strong></div>
                    <div><span>来源</span><strong>{{ detail.source || '—' }}</strong></div>
                    <div><span>上传人</span><strong>{{ detail.uploaded_by }}</strong></div>
                    <div><span>上传时间</span><strong>{{ fmtDate(detail.created_at) }}</strong></div>
                    <div><span>大小</span><strong>{{ fmtSize((detail as {size_bytes:number}).size_bytes) }}</strong></div>
                    <div><span>编码</span><strong>{{ (detail as {encoding?:string}).encoding || '—' }}</strong></div>
                    <div class="meta-full"><span>文件名</span><code>{{ detail.filename }}</code></div>
                    <div class="meta-full"><span>存储路径</span><code>{{ detail.storage_path }}</code></div>
                  </div>
                </div>

                <!-- Binary file: render a Huawei-style info card with download / preview -->
                <template v-if="(detail as {is_binary?:boolean}).is_binary">
                  <div v-if="isPreviewableExt(detailFileExt)" class="binary-card binary-previewable">
                    <div class="binary-head">
                      <el-icon size="22" class="binary-head-icon"><Folder /></el-icon>
                      <div>
                        <div class="binary-name">{{ detail.filename }}</div>
                        <div class="binary-meta">
                          {{ fmtSize((detail as {size_bytes:number}).size_bytes) }}
                          · {{ detailFileExt.toUpperCase() }} 文件
                        </div>
                      </div>
                      <el-button :icon="Download" size="small" @click="download(detail)">下载原文件</el-button>
                    </div>
                    <DocxViewer
                      :doc-id="(detail as {id:number}).id"
                      :ext="detailFileExt"
                      :title="(detail as {title:string}).title"
                      :filename="(detail as {filename:string}).filename"
                      @download="download(detail)"
                    />
                  </div>
                  <div v-if="isChmExt" class="binary-card binary-card-chm">
                    <div class="binary-icon">
                      <el-icon size="48" color="#1caf6e"><Position /></el-icon>
                    </div>
                    <div class="binary-info">
                      <div class="binary-name">{{ detail.filename }}</div>
                      <div class="binary-meta">
                        {{ fmtSize((detail as {size_bytes:number}).size_bytes) }}
                        · CHM (Microsoft Compiled HTML Help)
                      </div>
                      <div class="binary-tip">
                        本平台<b>没有内置 CHM 解包依赖</b>，浏览器也无法直接渲染 CHM。
                        推荐做法：点击下方按钮打开<b>{{ officialDocLink().vendor }} 官方在线产品文档</b>，
                        内容与 CHM 一致，由厂商服务器提供，无需任何本地客户端。
                      </div>
                      <div class="chm-product">
                        <span class="chm-product-label">识别到的产品型号：</span>
                        <el-tag
                          size="small"
                          type="success"
                          effect="plain"
                        >{{ parseProductKey((detail as {filename:string}).filename, (detail as {title:string}).title).family || '未识别' }}</el-tag>
                        <el-tag
                          v-if="parseProductKey((detail as {filename:string}).filename, (detail as {title:string}).title).version"
                          size="small"
                          type="info"
                          effect="plain"
                        >{{ parseProductKey((detail as {filename:string}).filename, (detail as {title:string}).title).version }}</el-tag>
                      </div>
                      <div class="chm-link-row">
                        <span class="chm-link-label">跳转链接：</span>
                        <a
                          class="chm-link"
                          :href="officialDocLink().url"
                          target="_blank"
                          rel="noopener noreferrer"
                        >{{ officialDocLink().url }}</a>
                      </div>
                      <div class="binary-actions">
                        <el-button type="success" size="default" @click="openOfficialDoc">打开官方在线文档</el-button>
                        <el-button :icon="Download" @click="download(detail)">下载本地 CHM</el-button>
                        <el-button
                          v-if="isWindows"
                          type="warning"
                          plain
                          @click="openLocalChm"
                        >
                          {{ isChmHandlerRegistered((detail as { id: number }).id) ? '在本机打开' : '用本地程序打开（hh.exe）' }}
                        </el-button>
                        <el-button
                          v-if="isWindows && isChmHandlerRegistered((detail as { id: number }).id)"
                          size="small"
                          link
                          type="info"
                          @click="resetChmHandlerRegistration((detail as { id: number }).id)"
                        >重置注册</el-button>
                        <el-button :icon="CopyDocument" plain @click="copyAiPrompt">复制 AI 询问模板</el-button>
                      </div>
                    </div>
                  </div>
                  <div v-else class="binary-card">
                    <div class="binary-icon">
                      <el-icon size="48"><Folder /></el-icon>
                    </div>
                    <div class="binary-info">
                      <div class="binary-name">{{ detail.filename }}</div>
                      <div class="binary-meta">
                        {{ fmtSize((detail as {size_bytes:number}).size_bytes) }}
                        <span v-if="detailFileExt"> · {{ detailFileExt.toUpperCase() }} 文件</span>
                      </div>
                      <div class="binary-tip">
                        此文件为二进制格式（PDF / Word / 压缩包 等），无法直接预览文本内容。
                      </div>
                      <div class="binary-actions">
                        <el-button type="primary" :icon="Download" @click="download(detail)">下载到本地</el-button>
                      </div>
                    </div>
                  </div>
                </template>

                <!-- Text content: intro + visible sections -->
                <template v-else>
                  <!-- Office / vendor formats that *look* like text to the
                       backend container-reader (e.g. .docx → markdown from
                       word/document.xml) but should be rendered with their
                       original layout.  We treat them as binary for preview
                       purposes and let DocxViewer rehydrate them. -->
                  <div v-if="isPreviewableExt(detailFileExt)" class="binary-card binary-previewable">
                    <div class="binary-head">
                      <el-icon size="22" class="binary-head-icon"><Folder /></el-icon>
                      <div>
                        <div class="binary-name">{{ detail.filename }}</div>
                        <div class="binary-meta">
                          {{ fmtSize((detail as {size_bytes:number}).size_bytes) }}
                          · {{ detailFileExt.toUpperCase() }} 文件
                        </div>
                      </div>
                      <el-button :icon="Download" size="small" @click="download(detail)">下载原文件</el-button>
                    </div>
                    <DocxViewer
                      :doc-id="(detail as {id:number}).id"
                      :ext="detailFileExt"
                      :title="(detail as {title:string}).title"
                      :filename="(detail as {filename:string}).filename"
                      @download="download(detail)"
                    />
                  </div>
                  <template v-else>
                    <div class="manual-intro" v-if="introHtml" v-html="introHtml" />
                    <section
                      v-for="s in visibleSections"
                      :key="s.id"
                      :id="s.id"
                      class="manual-section"
                      :class="{ 'is-active': activeSectionId === s.id }"
                    >
                    <header class="section-header">
                      <h2 class="section-title">{{ s.title }}</h2>
                      <div class="section-path" v-if="s.group">
                        <el-icon><Folder /></el-icon>
                        <span>{{ s.group }}</span>
                        <span class="path-sep">/</span>
                        <b>{{ s.title }}</b>
                      </div>
                    </header>
                    <div
                      class="section-body markdown-body"
                      v-html="renderMarkdown(s.body)"
                    />
                  </section>

                  <!-- Infrastructure / metadata files (profile, navi, …) — collapsed -->
                  <el-collapse v-if="metaSections.length" class="meta-collapse">
                    <el-collapse-item :title="`原始资源文件（${metaSections.length}）`" name="meta">
                      <div class="meta-list">
                        <div v-for="s in metaSections" :key="s.id" class="meta-list-item">
                          <el-icon><Folder /></el-icon>
                          <code>{{ s.rawTitle }}</code>
                          <span class="meta-size">{{ s.body.length }} 字</span>
                        </div>
                      </div>
                    </el-collapse-item>
                  </el-collapse>

                  <el-empty
                    v-if="!visibleSections.length && !introHtml"
                    description="该文档暂无文本内容（可能为二进制文件）"
                    :image-size="80"
                  />
                  </template>
                </template>

                <!-- Back-to-top floating button -->
                <transition name="fade">
                  <button v-if="showScrollTop" class="scroll-top-btn" @click="scrollToTop" title="返回顶部">
                    ↑
                  </button>
                </transition>
              </main>
            </div>
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>

    <!-- ── upload progress dialog ── -->
    <el-dialog
      v-model="uploadDialog.open"
      title="正在上传"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      :show-close="false"
      width="480px"
      align-center
    >
      <div class="upload-progress">
        <div class="upload-file-info">
          <el-icon class="upload-file-icon"><folder /></el-icon>
          <div class="upload-file-text">
            <div class="upload-file-name">{{ uploadDialog.fileName }}</div>
            <div class="upload-file-size">{{ fmtSize(uploadDialog.fileSize) }}</div>
          </div>
        </div>
        <el-progress
          :percentage="uploadDialog.percent"
          :stroke-width="14"
          :status="uploadDialog.percent === 100 ? 'success' : ''"
        />
        <div class="upload-progress-hint">
          <span v-if="uploadDialog.percent < 100">上传中，请稍候…</span>
          <span v-else>上传完成，准备跳转列表…</span>
        </div>
      </div>
    </el-dialog>

    <!-- ── edit dialog ── -->
    <el-dialog
      v-model="editDialog.open"
      title="编辑文档元数据"
      width="540px"
      align-center
    >
      <el-form label-width="90px">
        <el-form-item label="标题" required>
          <el-input v-model="editDialog.title" />
        </el-form-item>
        <el-form-item label="厂商" required>
          <el-select
            v-model="editDialog.vendor"
            filterable
            allow-create
            default-first-option
            clearable
            style="width:100%"
          >
            <el-option v-for="v in VENDORS" :key="v" :label="v" :value="v" />
          </el-select>
        </el-form-item>
        <el-form-item label="分类">
          <el-input :model-value="DEFAULT_CATEGORY" readonly disabled />
        </el-form-item>
        <el-form-item label="版本">
          <el-input v-model="editDialog.version_tag" />
        </el-form-item>
        <el-form-item label="来源">
          <el-input v-model="editDialog.source" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDialog.open = false">取消</el-button>
        <el-button type="primary" :loading="editDialog.saving" @click="submitEdit">保存（同步到 Obsidian）</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.page-header {
  background: var(--bg-canvas);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--text-1);
}
.page-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-1);
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.page-title::before {
  content: '';
  width: 3px;
  height: 14px;
  background: var(--accent);
  border-radius: 2px;
}
.header-right { display: flex; gap: 10px; }

.tabs-card {
  background: var(--bg-canvas);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  padding: 6px 18px 18px;
}
.tabs-card :deep(.el-tabs__nav-wrap)::after {
  background: var(--border-1) !important;
}
.tabs-card :deep(.el-tabs__header) {
  margin: 0 0 14px;
}
.tabs-card :deep(.el-tabs__item) {
  padding: 0 16px 0 0 !important;
  margin-right: 18px !important;
}
.tabs-card :deep(.el-tabs__item:first-child) {
  padding-left: 4px !important;
}

.hit-card {
  padding: 14px;
  border-bottom: 1px solid var(--border-1);
  cursor: pointer;
  color: var(--text-1);
}
.hit-card:hover { background: var(--bg-elev); }
.hit-title { font-weight: 600; color: var(--accent); font-size: 14px; margin-bottom: 4px; }
.hit-meta { font-size: 12px; color: var(--text-3); margin-bottom: 8px; }
.hit-snippet {
  background: var(--bg-input);
  padding: 8px;
  font-size: 12px;
  border-radius: var(--radius-sm);
  max-height: 100px;
  overflow: auto;
  font-family: var(--font-mono);
  color: var(--text-1);
}

/* ── Manual layout (Huawei-style product manual page) ───────────────── */
.manual-layout {
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: var(--bg-canvas);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  padding: 14px 18px 18px;
  min-height: calc(100vh - 200px);
}

.manual-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border-1);
  gap: 16px;
  flex-wrap: wrap;
}
.manual-crumbs {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--text-2);
  min-width: 0;
  flex: 1;
}
.crumb {
  color: var(--text-2);
  padding: 2px 4px;
  border-radius: 3px;
  white-space: nowrap;
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.crumb-link {
  cursor: pointer;
  color: var(--accent);
  transition: color 0.15s;
}
.crumb-link:hover { color: #67e8f9; text-decoration: underline; }
.crumb-current { color: var(--text-1); font-weight: 600; }
.crumb-sep { color: var(--text-3); font-family: var(--font-mono); }

.manual-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.toolbar-spacer { width: 8px; }
.health-pill { letter-spacing: .5px; font-weight: 600; }

.manual-body {
  display: grid;
  grid-template-columns: 260px 1fr;
  gap: 16px;
  flex: 1;
  min-height: 0;
}
/* When there's no TOC (binary file, e.g. PDF) collapse to a single
   column so the content pane gets the full width.  Without this the
   260px sidebar track stays reserved and the PDF iframe is squeezed
   into a thin sliver. */
.manual-body:has(.manual-content:only-child) {
  grid-template-columns: 1fr;
}

/* TOC sidebar */
.manual-toc {
  display: flex;
  flex-direction: column;
  background: var(--bg-input);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  padding: 10px;
  position: sticky;
  top: 0;
  align-self: flex-start;
  max-height: calc(100vh - 220px);
}
.toc-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-1);
  padding: 4px 4px 10px;
  border-bottom: 1px solid var(--border-1);
  margin-bottom: 8px;
}
.toc-count {
  margin-left: auto;
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-3);
  background: var(--bg-canvas);
  padding: 1px 6px;
  border-radius: 8px;
  border: 1px solid var(--border-1);
}
.toc-search { margin-bottom: 8px; }
.toc-search :deep(.el-input__wrapper) {
  background: var(--bg-canvas);
  border-radius: var(--radius-sm);
}
.toc-list {
  list-style: none;
  margin: 0;
  padding: 0;
  overflow-y: auto;
  flex: 1;
  min-height: 0;
}
.toc-folder {
  padding: 10px 10px 4px;
  font-family: var(--font-mono);
  font-size: 10.5px;
  font-weight: 600;
  color: var(--text-3);
  letter-spacing: 0.5px;
  text-transform: uppercase;
  border-top: 1px dashed var(--border-1);
  margin-top: 4px;
}
.toc-folder:first-child { border-top: none; margin-top: 0; }
.toc-item {
  padding: 7px 10px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  margin-bottom: 2px;
  border-left: 2px solid transparent;
  transition: all 0.12s ease;
}
.toc-item:hover {
  background: var(--bg-canvas);
  border-left-color: var(--border-3);
}
.toc-item.active {
  background: var(--accent-soft);
  border-left-color: var(--accent);
}
.toc-item-title {
  font-size: 12.5px;
  color: var(--text-1);
  font-weight: 500;
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.toc-item.active .toc-item-title { color: var(--accent); }
.toc-item-sub {
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-3);
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Content pane */
.manual-content {
  background: var(--bg-input);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  padding: 18px 22px 32px;
  overflow-y: auto;
  max-height: calc(100vh - 220px);
  scroll-behavior: smooth;
  min-width: 0; /* let the grid track shrink instead of pushing the iframe into a sliver */
}

.manual-meta-card {
  background: var(--bg-canvas);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  padding: 12px 16px;
  margin-bottom: 18px;
}
.meta-card-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px 24px;
  font-size: 12.5px;
}
.meta-card-grid .meta-full { grid-column: 1 / -1; }
.meta-card-grid > div { display: flex; align-items: center; gap: 6px; }
.meta-card-grid span {
  color: var(--text-3);
  font-size: 12px;
  min-width: 56px;
  text-align: right;
}
.meta-card-grid strong {
  color: var(--text-1);
  font-weight: 500;
  flex: 1;
  word-break: break-all;
}
.meta-card-grid code {
  color: var(--text-1);
  font-family: var(--font-mono);
  font-size: 11.5px;
  background: var(--bg-input);
  padding: 1px 6px;
  border-radius: 3px;
  border: 1px solid var(--border-1);
}

.manual-intro {
  font-size: 13px;
  color: var(--text-2);
  margin-bottom: 24px;
  padding-bottom: 16px;
  border-bottom: 1px dashed var(--border-1);
}

.manual-section {
  margin-bottom: 28px;
  padding: 4px 0 16px;
  border-radius: var(--radius-md);
  transition: background 0.2s ease;
  scroll-margin-top: 12px;
}
.manual-section.is-active {
  background: var(--accent-soft);
  margin-left: -10px;
  margin-right: -10px;
  padding-left: 10px;
  padding-right: 10px;
}
.section-header {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 14px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border-1);
}
.section-title {
  margin: 0;
  font-size: 17px;
  font-weight: 700;
  color: var(--text-1);
  letter-spacing: 0.3px;
}
.section-path {
  display: flex;
  align-items: center;
  gap: 6px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-3);
}
.section-path .path-sep { color: var(--border-3); margin: 0 2px; }
.section-path b { color: var(--text-2); font-weight: 500; }

/* Markdown body styles (rendered via marked) */
.markdown-body {
  font-size: 13.5px;
  line-height: 1.7;
  color: var(--text-1);
  word-break: break-word;
}
.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3),
.markdown-body :deep(h4) {
  margin: 1em 0 0.5em;
  color: var(--text-1);
  font-weight: 700;
  line-height: 1.3;
}
.markdown-body :deep(h1) { font-size: 22px; padding-bottom: 8px; border-bottom: 2px solid var(--border-1); }
.markdown-body :deep(h2) { font-size: 18px; }
.markdown-body :deep(h3) { font-size: 16px; }
.markdown-body :deep(h4) { font-size: 14px; }
.markdown-body :deep(p) { margin: 0.6em 0; }
.markdown-body :deep(ul),
.markdown-body :deep(ol) { margin: 0.6em 0; padding-left: 1.6em; }
.markdown-body :deep(li) { margin: 0.2em 0; }
.markdown-body :deep(strong) { color: var(--accent); font-weight: 600; }
.markdown-body :deep(em) { color: var(--text-2); }
.markdown-body :deep(a) { color: var(--accent); text-decoration: none; border-bottom: 1px dashed var(--accent); }
.markdown-body :deep(a:hover) { background: var(--accent-soft); }
.markdown-body :deep(blockquote) {
  margin: 0.8em 0;
  padding: 8px 14px;
  border-left: 3px solid var(--accent);
  background: var(--bg-canvas);
  color: var(--text-2);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
}
.markdown-body :deep(code) {
  font-family: var(--font-mono);
  font-size: 12px;
  background: var(--bg-canvas);
  border: 1px solid var(--border-1);
  padding: 1px 5px;
  border-radius: 3px;
  color: var(--accent);
}
.markdown-body :deep(pre) {
  background: #0b1220;
  border: 1px solid var(--border-2);
  border-radius: var(--radius-md);
  padding: 12px 14px;
  margin: 0.8em 0;
  overflow-x: auto;
  font-family: var(--font-mono);
  font-size: 12.5px;
  line-height: 1.6;
  color: #d1e3f8;
}
.markdown-body :deep(pre code) {
  background: transparent;
  border: none;
  padding: 0;
  color: inherit;
  font-size: 12.5px;
}
.markdown-body :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 0.8em 0;
  font-size: 12.5px;
}
.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid var(--border-1);
  padding: 6px 10px;
  text-align: left;
}
.markdown-body :deep(th) {
  background: var(--bg-canvas);
  color: var(--text-2);
  font-weight: 600;
}
.markdown-body :deep(hr) {
  border: none;
  border-top: 1px dashed var(--border-2);
  margin: 1.5em 0;
}
.markdown-body :deep(img) {
  max-width: 100%;
  border-radius: var(--radius-sm);
}

/* Binary file card */
.binary-card {
  display: flex;
  gap: 18px;
  padding: 22px;
  background: linear-gradient(135deg, var(--bg-canvas), var(--bg-input));
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  align-items: center;
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
}
.binary-card.binary-previewable {
  display: block;
  padding: 0;
  overflow: hidden;
  width: 100%;
}

/* CHM-specific card — same skeleton as binary-card but with a warmer
   accent so the operator immediately knows this format needs extra
   steps (not just a generic "download and you're done" message). */
.binary-card-chm .binary-icon { background: rgba(230, 162, 60, .12); }
.binary-card-chm .binary-name { color: var(--text-1); }
.binary-card-chm .binary-icon { background: rgba(28, 175, 110, .12); }
.binary-card-chm .chm-product {
  display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
  margin: 8px 0 10px;
}
.binary-card-chm .chm-product-label {
  font-size: 11px;
  color: var(--text-3);
  margin-right: 2px;
}
.binary-card-chm .chm-link-row {
  display: flex; align-items: flex-start; gap: 8px;
  margin: 6px 0 14px;
  padding: 8px 10px;
  background: var(--bg-input);
  border: 1px dashed var(--border-1);
  border-radius: 6px;
}
.binary-card-chm .chm-link-label {
  font-size: 11px;
  color: var(--text-3);
  flex-shrink: 0;
  padding-top: 1px;
}
.binary-card-chm .chm-link {
  font: 11px var(--font-mono);
  color: var(--accent);
  word-break: break-all;
  text-decoration: none;
  border-bottom: 1px dotted var(--accent);
}
.binary-card-chm .chm-link:hover { border-bottom-style: solid; }
.binary-card-chm .binary-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.binary-card .binary-head {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 18px;
  border-bottom: 1px solid var(--border-1);
  background: rgba(255,255,255,.02);
  min-width: 0;
  width: 100%;
  box-sizing: border-box;
  flex-wrap: nowrap;
}
.binary-card .binary-head > div {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
}
.binary-card .binary-head-icon { color: var(--accent); flex-shrink: 0; }
.binary-card .binary-head .binary-name {
  font-weight: 600;
  color: var(--text-1);
  white-space: normal;
  /* Don't break-all — that splits Chinese characters one per line.
     Use overflow-wrap:anywhere so the *break* happens only when
     there's no other choice (e.g. a 200-char path with no spaces),
     and only at word-ish boundaries where possible. */
  overflow-wrap: break-word;
  word-break: normal;
  line-height: 1.4;
  max-width: 100%;
}
.binary-card .binary-head .binary-meta {
  font-size: 12px;
  color: var(--text-3);
  margin-top: 2px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
}
.binary-icon {
  width: 80px;
  height: 80px;
  background: var(--accent-soft);
  border: 1px solid var(--border-2);
  border-radius: var(--radius-md);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--accent);
  flex-shrink: 0;
}
.binary-info { flex: 1; min-width: 0; }
.binary-name {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-1);
  word-break: break-word; /* was break-all — splits Chinese chars */
  margin-bottom: 6px;
}
.binary-meta {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-3);
  margin-bottom: 8px;
}
.binary-tip {
  font-size: 12.5px;
  color: var(--text-2);
  margin-bottom: 12px;
  line-height: 1.5;
}
.binary-actions { display: flex; gap: 8px; }

/* Floating back-to-top button inside the content pane */
.scroll-top-btn {
  position: sticky;
  bottom: 18px;
  margin-left: auto;
  display: block;
  width: 36px;
  height: 36px;
  border-radius: 50%;
  background: var(--accent);
  color: #0b1220;
  border: none;
  font-size: 18px;
  font-weight: 700;
  cursor: pointer;
  box-shadow: 0 6px 18px rgba(14, 165, 233, 0.35);
  transition: transform 0.15s ease;
}
.scroll-top-btn:hover { transform: translateY(-2px); }

.fade-enter-active, .fade-leave-active { transition: opacity 0.18s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

/* Collapsed infrastructure files panel */
.meta-collapse {
  margin-top: 18px;
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  background: var(--bg-canvas);
}
.meta-collapse :deep(.el-collapse-item__header) {
  padding: 0 14px;
  font-size: 12.5px;
  color: var(--text-2);
  border-bottom: none;
}
.meta-collapse :deep(.el-collapse-item__content) { padding: 0 14px 12px; }
.meta-list { display: flex; flex-direction: column; gap: 6px; }
.meta-list-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-2);
  padding: 4px 6px;
  background: var(--bg-input);
  border-radius: var(--radius-sm);
}
.meta-list-item code {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text-1);
  flex: 1;
  word-break: break-all;
}
.meta-size {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-3);
  flex-shrink: 0;
}

.upload-hint {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 14px;
  color: var(--text-2);
}
.upload-icon {
  font-size: 32px;
  color: var(--accent);
}
.upload-sub {
  font-size: 12px;
  color: var(--text-3);
  max-width: 480px;
  text-align: center;
}
.pending-info {
  font-size: 14px;
  color: var(--text-1);
}
.pending-size {
  font-size: 12px;
  color: var(--text-3);
  margin-left: 4px;
}

.upload-progress {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 8px 4px;
}
.upload-file-info {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  background: var(--bg-input);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
}
.upload-file-icon {
  font-size: 28px;
  color: var(--accent);
}
.upload-file-text { flex: 1; min-width: 0; }
.upload-file-name {
  font-size: 14px;
  color: var(--text-1);
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.upload-file-size {
  font-size: 12px;
  color: var(--text-3);
  margin-top: 2px;
}
.upload-progress-hint {
  text-align: center;
  font-size: 12.5px;
  color: var(--text-3);
}
</style>
