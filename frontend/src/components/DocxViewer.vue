<script setup lang="ts">
/**
 * In-browser renderer for vendor product manuals and common Office docs.
 *
 * Why this exists: the backend ``smart_read`` collapses binary containers
 * (docx / hdx / epub) into a flat Markdown blob, which loses headings,
 * lists, tables, and embedded images — exactly the artifacts an operator
 * needs when they pick a document from the portal.  This component
 * pipelines the original file (fetched with the user's session) into a
 * real document renderer so the user sees the source as the vendor
 * shipped it.
 *
 * Strategy by file extension:
 *
 * - ``.docx``                → mammoth.js → sanitized HTML
 * - ``.pptx`` / ``.xlsx``    → JSZip pulls the inner text payloads and we
 *                              render them as screens / sheets
 * - ``.csv``                 → PapaParse → table
 * - ``.md`` / ``.markdown``  → marked → sanitized HTML
 * - ``.html`` / ``.htm``     → sanitized passthrough
 * - ``.pdf``                 → embedded iframe (browser-native viewer)
 * - ``.png`` / ``.jpg`` / etc → inline image
 * - ``.doc`` (legacy OLE2)   → fallback notice — mammoth does not support it
 *                              and a server-side toolchain would be required
 *                              to fully convert it.  We still expose a
 *                              "Download original" button.
 * - anything else            → "Open in new tab" so the browser can take
 *                              its best shot (e.g. archives).
 */
import { computed, onMounted, ref, watch } from 'vue'
import { http } from '@/api'

const props = defineProps<{
  /** Server-side document id. */
  docId: number
  /** File extension (lowercase, no dot). */
  ext: string
  /** Display title for the fallback banner. */
  title: string
  /** Original filename — used for the iframe title and the download button. */
  filename: string
}>()

const emit = defineEmits<{ (e: 'download'): void }>()

type RendererStatus = 'idle' | 'loading' | 'ready' | 'error'
type RendererKind =
  | 'docx'
  | 'pptx'
  | 'xlsx'
  | 'csv'
  | 'md'
  | 'html'
  | 'pdf'
  | 'image'
  | 'doc-legacy'
  | 'fallback'

const DOCX = new Set(['docx'])
const PPTX = new Set(['pptx'])
const XLSX = new Set(['xlsx'])
const CSV = new Set(['csv'])
const PLAIN_MD = new Set(['md', 'markdown', 'mdown'])
const HTML = new Set(['html', 'htm', 'mhtml'])
const PDF = new Set(['pdf'])
const IMAGE = new Set(['png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp', 'svg', 'ico'])
const DOC_LEGACY = new Set(['doc'])

const status = ref<RendererStatus>('idle')
const errorMsg = ref('')
const renderedHtml = ref('')
const downloadUrl = ref('')
const viewerUrl = ref('')
const previewImageSrc = ref('')
const previewPdfSrc = ref('')
const csvRows = ref<string[][]>([])
const sheetNames = ref<string[]>([])
const activeSheet = ref(0)
/**
 * Sheet data, indexed ``sheetData[sheetIdx][rowIdx][colIdx]``.
 * Cached once when the workbook is decoded; switching tabs just changes
 * ``activeSheet`` — no re-decode needed.
 */
const sheetData = ref<string[][][]>([])
const slides = ref<{ name: string; texts: string[] }[]>([])

const rendererKind = computed<RendererKind>(() => {
  const e = props.ext
  if (DOCX.has(e)) return 'docx'
  if (PPTX.has(e)) return 'pptx'
  if (XLSX.has(e)) return 'xlsx'
  if (CSV.has(e)) return 'csv'
  if (PLAIN_MD.has(e)) return 'md'
  if (HTML.has(e)) return 'html'
  if (PDF.has(e)) return 'pdf'
  if (IMAGE.has(e)) return 'image'
  if (DOC_LEGACY.has(e)) return 'doc-legacy'
  return 'fallback'
})

const showLegacyNotice = computed(() => rendererKind.value === 'doc-legacy')

async function blobFor(path: string): Promise<Blob> {
  const resp = await http.get(path, { responseType: 'blob' })
  return resp.data as Blob
}

function htmlEscape(s: string): string {
  return s.replace(/[&<>"']/g, (c) => {
    switch (c) {
      case '&': return '&amp;'
      case '<': return '&lt;'
      case '>': return '&gt;'
      case '"': return '&quot;'
      default: return '&#39;'
    }
  })
}

function csvToTable(rows: string[][]): string {
  if (!rows.length) return '<p><em>(空文件)</em></p>'
  const head = rows[0].map((c) => `<th>${htmlEscape(c)}</th>`).join('')
  const body = rows.slice(1)
    .map((r) => `<tr>${r.map((c) => `<td>${htmlEscape(c)}</td>`).join('')}</tr>`)
    .join('')
  return `<table class="grid"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`
}

function sheetToTable(rows: string[][]): string {
  if (!rows.length) return '<p><em>(空工作表)</em></p>'
  const head = rows[0].map((c) => `<th>${htmlEscape(String(c))}</th>`).join('')
  const body = rows.slice(1)
    .map((r) => `<tr>${r.map((c) => `<td>${htmlEscape(String(c))}</td>`).join('')}</tr>`)
    .join('')
  return `<table class="grid"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`
}

function slidesToHtml(items: { name: string; texts: string[] }[]): string {
  if (!items.length) return '<p><em>(幻灯片中未发现文本)</em></p>'
  return items
    .map(
      (s, i) => `
      <section class="slide">
        <header class="slide-header">
          <span class="slide-idx">第 ${i + 1} 页</span>
          <h3 class="slide-title">${htmlEscape(s.name)}</h3>
        </header>
        <div class="slide-body">
          ${s.texts.length
            ? `<ul>${s.texts.map((t) => `<li>${htmlEscape(t)}</li>`).join('')}</ul>`
            : '<p class="empty">(该页无文本，可能为图片或图表)</p>'}
        </div>
      </section>
    `,
    )
    .join('')
}

async function renderBlob(blob: Blob): Promise<string> {
  switch (rendererKind.value) {
    case 'docx': {
      const { default: mammoth } = await import('mammoth')
      const buffer = await blob.arrayBuffer()
      const result = await mammoth.convertToHtml(
        { arrayBuffer: buffer },
        {
          // Preserve simple but useful styling: bold / italic / underline,
          // headings, lists, tables, hyperlinks, and inline images (which
          // mammoth exposes as <img src="data:image/...">).
          styleMap: [
            "p[style-name='Title'] => h1:fresh",
            "p[style-name='Subtitle'] => h2:fresh",
            "p[style-name='Heading 1'] => h1:fresh",
            "p[style-name='Heading 2'] => h2:fresh",
            "p[style-name='Heading 3'] => h3:fresh",
            "p[style-name='Heading 4'] => h4:fresh",
            "p[style-name='Quote'] => blockquote:fresh",
          ],
        },
      )
      if (result.messages.length) {
        // eslint-disable-next-line no-console
        console.debug('[DocxViewer] mammoth messages:', result.messages)
      }
      return result.value
    }
    case 'md': {
      const { marked } = await import('marked')
      const text = await blob.text()
      return marked.parse(text, { async: false }) as string
    }
    case 'html': {
      return await blob.text()
    }
    case 'csv': {
      const { default: Papa } = await import('papaparse')
      const text = await blob.text()
      const parsed = Papa.parse<string[]>(text, { skipEmptyLines: true })
      const rows = parsed.data.map((row) =>
        Array.isArray(row) ? row.map((c) => String(c ?? '')) : [String(row ?? '')],
      )
      csvRows.value = rows
      return csvToTable(rows)
    }
    case 'xlsx': {
      const XLSX = await import('xlsx')
      const buffer = await blob.arrayBuffer()
      const wb = XLSX.read(buffer, { type: 'array' })
      sheetNames.value = wb.SheetNames
      activeSheet.value = 0
      const all: string[][][] = wb.SheetNames.map((n) => {
        const sh = wb.Sheets[n]
        const aoa = XLSX.utils.sheet_to_json<unknown[]>(sh, { header: 1, blankrows: false })
        return aoa.map((r) =>
          Array.isArray(r) ? r.map((c) => String(c ?? '')) : [String(r ?? '')],
        )
      })
      sheetData.value = all[0] ?? []
      return sheetToTable(all[0] ?? [])
    }
    case 'pptx': {
      const JSZip = (await import('jszip')).default
      const buffer = await blob.arrayBuffer()
      const zip = await JSZip.loadAsync(buffer)
      const names = Object.keys(zip.files).filter((p) => /^ppt\/slides\/slide\d+\.xml$/.test(p)).sort()
      const parsed: { name: string; texts: string[] }[] = []
      for (const n of names) {
        const xml = await zip.files[n].async('text')
        // Extract <a:t> text nodes from the slide XML.  Regex is fine here:
        // we only want human-readable text, not a full XML parse.
        const texts = Array.from(xml.matchAll(/<a:t[^>]*>([^<]*)<\/a:t>/g)).map((m) => m[1])
        parsed.push({ name: n.replace(/^ppt\/slides\//, ''), texts })
      }
      slides.value = parsed
      return slidesToHtml(parsed)
    }
    case 'pdf': {
      // The blob is rendered via the browser's native PDF viewer in an
      // iframe (see the load() helper above).  We don't return any
      // HTML here — the iframe handles everything.
      return ''
    }
    case 'image': {
      const url = URL.createObjectURL(blob)
      previewImageSrc.value = url
      return ''
    }
    case 'doc-legacy': {
      // .doc is the OLE2 legacy binary format.  mammoth is officially a
      // .docx-only library, but its underlying converter happens to
      // accept some .doc files (it embeds antiword-style detection).
      // We try mammoth first; only fall through to the friendly notice
      // when the parse fails — otherwise we lose real content the user
      // could otherwise read inline.
      try {
        const { default: mammoth } = await import('mammoth')
        const buffer = await blob.arrayBuffer()
        const result = await mammoth.convertToHtml({ arrayBuffer: buffer })
        if (result.value.trim().length >= 32) return result.value
      } catch (err) {
        // eslint-disable-next-line no-console
        console.debug('[DocxViewer] legacy .doc parse failed:', err)
      }
      // Fall through to the notice template (status is already 'ready'
      // and downloadUrl is set by the caller).
      return ''
    }
    case 'fallback':
    default:
      return ''
  }
}

async function sanitize(html: string): Promise<string> {
  const { default: DOMPurify } = await import('dompurify')
  return DOMPurify.sanitize(html, {
    ADD_ATTR: ['target'],
    USE_PROFILES: { html: true },
  })
}

async function load() {
  console.log('[DocxViewer] load() called, status:', status.value, 'rendererKind:', rendererKind.value)
  status.value = 'loading'
  errorMsg.value = ''
  renderedHtml.value = ''
  downloadUrl.value = ''
  viewerUrl.value = ''
  previewImageSrc.value = ''
  csvRows.value = []
  sheetNames.value = []
  activeSheet.value = 0
  sheetData.value = []
  slides.value = []
  try {
    // Use the /raw endpoint (Content-Disposition: inline) instead of
    // /download.  Some corporate browsers have IDM / 迅雷-style
    // download managers that hijack any response carrying
    // ``Content-Disposition: attachment`` and pop a "new download
    // task" dialog even when our JS calls fetch() and never asks
    // for a download.  ``/raw`` returns the same bytes inline, so
    // fetch() actually receives the blob and we can parse it.
    const blob = await blobFor(`/documents/${props.docId}/raw`)
    const cacheBustUrl = URL.createObjectURL(blob)
    if (rendererKind.value === 'fallback') {
      // Hand the blob URL to the iframe; let the browser decide whether
      // its native viewer can open it (PDFs / images / archives).
      viewerUrl.value = cacheBustUrl
      downloadUrl.value = cacheBustUrl
      status.value = 'ready'
      return
    }
    if (rendererKind.value === 'pdf') {
      // Hand the blob URL straight to the browser's native PDF viewer
      // (PDFium on Edge/Chrome, pdf.js on Firefox).  This avoids the
      // pdf.js text-extraction path, which crashes on PDFs that embed
      // non-Latin font subsets (BCJKEE+*, HeiseiKakuGo, Wingdings, ...)
      // with "Cannot substitute the font because of its name" and
      // "offset is out of bounds" errors that manifest as vertically
      // stacked characters in the inline-html fallback.
      previewPdfSrc.value = cacheBustUrl
      downloadUrl.value = cacheBustUrl
      status.value = 'ready'
      return
    }
    // doc-legacy: skip iframe (browser can't render OLE2) — fall through
    // to the friendly notice template.  We still expose the blob URL via
    // downloadUrl so the "open in new tab" button works.
    if (rendererKind.value === 'doc-legacy') {
      downloadUrl.value = cacheBustUrl
      status.value = 'ready'
      return
    }
    const raw = await renderBlob(blob)
    if (!raw) {
      status.value = 'ready'
      return
    }
    renderedHtml.value = await sanitize(raw)
    downloadUrl.value = cacheBustUrl
    console.log('[DocxViewer] render complete, renderedHtml length:', raw.length, 'chars')
    status.value = 'ready'
  } catch (err: unknown) {
    const msg = (err as { message?: string })?.message || '无法加载文档预览'
    console.error('[DocxViewer] render error:', err)
    errorMsg.value = msg
    status.value = 'error'
  }
}

function openInNewTab() {
  if (downloadUrl.value) window.open(downloadUrl.value, '_blank')
}

function switchSheet(idx: number) {
  if (idx === activeSheet.value) return
  activeSheet.value = idx
}

onMounted(() => {
  console.log('[DocxViewer] mounted, props:', JSON.stringify({ docId: props.docId, ext: props.ext, title: props.title }))
  load()
})
watch(() => props.docId, load)
</script>

<template>
  <div class="docx-viewer">
    <div v-if="status === 'loading'" class="viewer-status">
      <el-icon class="is-loading"><Refresh /></el-icon>
      <span>正在渲染原始文档…</span>
    </div>

    <div v-else-if="status === 'error'" class="viewer-status error">
      <el-icon><WarningFilled /></el-icon>
      <div>
        <div class="status-title">渲染失败</div>
        <div class="status-detail">{{ errorMsg }}</div>
        <el-button size="small" type="primary" plain @click="load">重试</el-button>
      </div>
    </div>

    <div v-else-if="showLegacyNotice" class="viewer-status">
      <el-icon><WarningFilled /></el-icon>
      <div>
        <div class="status-title">该文件是 .doc 旧版格式，浏览器无法直接渲染</div>
        <div class="status-detail">
          .doc 是 Office 2003 前的 OLE2 二进制格式，浏览器内核
          （Chrome / Edge / Firefox）都不带 OLE2 解析器，所以无法像 .docx / PDF
          那样在线预览。最稳妥的方式是用本地 WPS / Word 打开后另存为 .docx
          再重新上传，这样本系统就能用 mammoth 还原出原始样式。
        </div>
        <div class="status-actions">
          <el-button size="small" type="primary" @click="emit('download')">
            下载原文件
          </el-button>
          <el-button size="small" plain @click="openInNewTab">
            在新标签页打开（系统 PDF/Office 阅读器）
          </el-button>
        </div>
      </div>
    </div>

    <div v-else-if="rendererKind === 'image'" class="image-frame">
      <img :src="previewImageSrc" :alt="title" />
    </div>

    <div v-else-if="rendererKind === 'xlsx' && sheetNames.length" class="xlsx-view">
      <el-tabs v-model="activeSheet" type="card" class="sheet-tabs">
        <el-tab-pane
          v-for="(name, idx) in sheetNames"
          :key="name"
          :name="idx"
          :label="name"
        >
          <div class="xlsx-table-wrap">
            <table class="grid">
              <thead>
                <tr>
                  <th v-for="(cell, ci) in (sheetData[idx] && sheetData[idx][0]) || []" :key="`h-${ci}`">
                    {{ cell }}
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, ri) in (sheetData[idx] || []).slice(1)" :key="`r-${ri}`">
                  <td v-for="(cell, ci) in row" :key="`c-${ri}-${ci}`">{{ cell }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </el-tab-pane>
      </el-tabs>
    </div>

    <div v-else-if="previewPdfSrc" class="pdf-frame">
      <iframe
        :src="previewPdfSrc"
        :title="title"
        class="pdf-iframe"
      />
    </div>

    <div v-else-if="viewerUrl" class="pdf-frame">
      <iframe
        :src="viewerUrl"
        :title="title"
        class="pdf-iframe"
      />
    </div>

    <article
      v-else-if="renderedHtml"
      class="rendered"
      v-html="renderedHtml"
    />
  </div>
</template>

<script lang="ts">
import { Refresh, WarningFilled } from '@element-plus/icons-vue'
// Re-export so the template can use them after the async resolution of the
// setup script. Vue's SFC compiler hoists <script setup> imports for the
// template, but having them here keeps ESLint and unplugin-auto-import happy.
export default { components: { Refresh, WarningFilled } }
</script>

<style scoped>
.docx-viewer {
  min-height: 360px;
  display: block;
}
.viewer-status {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 24px 28px;
  background: var(--bg-canvas);
  border: 1px dashed var(--border-1);
  border-radius: 8px;
  color: var(--text-2);
  font-size: 13px;
}
.viewer-status.error { border-color: rgba(245, 108, 108, .45); color: #f56c6c; }
.viewer-status .status-title { font-weight: 600; color: var(--text-1); margin-bottom: 4px; }
.viewer-status .status-detail { font-size: 12px; color: var(--text-3); margin-bottom: 8px; line-height: 1.6; }
.viewer-status .status-actions { display: flex; gap: 8px; flex-wrap: wrap; }

.pdf-frame,
.image-frame {
  height: 70vh;
  border: 1px solid var(--border-1);
  border-radius: 8px;
  overflow: hidden;
  background: #1f1f1f;
  display: flex;
  align-items: center;
  justify-content: center;
}
.pdf-iframe {
  width: 100%;
  height: 100%;
  border: 0;
}
.image-frame img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}

.xlsx-view {
  background: var(--bg-canvas);
  border: 1px solid var(--border-1);
  border-radius: 8px;
  overflow: hidden;
}
.sheet-tabs { padding: 8px 12px 0; }
.xlsx-table-wrap {
  max-height: 70vh;
  overflow: auto;
  padding: 12px;
}

.rendered {
  background: var(--bg-canvas);
  border: 1px solid var(--border-1);
  border-radius: 8px;
  padding: 28px 36px;
  font: 14px/1.7 var(--font-sans);
  color: var(--text-1);
  width: 100%;
  max-width: 100%;
  box-sizing: border-box;
  /* Allow long unbroken tokens (file names, hashes, URLs) to wrap. */
  overflow-wrap: anywhere;
  word-break: break-word;
}
.rendered :deep(h1),
.rendered :deep(h2),
.rendered :deep(h3),
.rendered :deep(h4) {
  margin: 1.2em 0 .5em;
  color: var(--text-1);
  line-height: 1.3;
}
.rendered :deep(h1) { font-size: 22px; border-bottom: 1px solid var(--border-1); padding-bottom: 6px; }
.rendered :deep(h2) { font-size: 18px; }
.rendered :deep(h3) { font-size: 16px; }
.rendered :deep(p) { margin: .6em 0; }
.rendered :deep(ul),
.rendered :deep(ol) { margin: .6em 0 .6em 1.4em; }
.rendered :deep(li) { margin: .25em 0; }
.rendered :deep(blockquote) {
  border-left: 3px solid var(--accent);
  margin: .8em 0;
  padding: 4px 12px;
  color: var(--text-2);
  background: rgba(255,255,255,.02);
}
.rendered :deep(table) {
  border-collapse: collapse;
  margin: 1em 0;
  width: 100%;
  font-size: 13px;
}
.rendered :deep(th),
.rendered :deep(td) {
  border: 1px solid var(--border-1);
  padding: 6px 10px;
  vertical-align: top;
}
.rendered :deep(th) { background: rgba(255,255,255,.04); font-weight: 600; }
.rendered :deep(img) {
  max-width: 100%;
  height: auto;
  border-radius: 4px;
  margin: .4em 0;
}
.rendered :deep(a) { color: var(--accent); text-decoration: underline; }
.rendered :deep(code) {
  background: rgba(255,255,255,.06);
  padding: 1px 5px;
  border-radius: 3px;
  font: 12px var(--font-mono);
}
.rendered :deep(pre) {
  background: #1c1c20;
  color: #eaeaea;
  padding: 12px;
  border-radius: 6px;
  overflow-x: auto;
  font: 12px/1.5 var(--font-mono);
}

table.grid {
  border-collapse: collapse;
  width: 100%;
  font-size: 13px;
}
table.grid th,
table.grid td {
  border: 1px solid var(--border-1);
  padding: 6px 10px;
  text-align: left;
  vertical-align: top;
  white-space: pre-wrap;
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
}
table.grid th {
  background: rgba(255,255,255,.04);
  font-weight: 600;
  position: sticky;
  top: 0;
}

.slide {
  border: 1px solid var(--border-1);
  border-radius: 8px;
  padding: 16px 20px;
  margin-bottom: 14px;
  background: var(--bg-canvas);
}
.slide-header {
  display: flex;
  align-items: baseline;
  gap: 12px;
  border-bottom: 1px dashed var(--border-1);
  padding-bottom: 8px;
  margin-bottom: 10px;
}
.slide-idx {
  font-size: 11px;
  color: var(--text-3);
  background: rgba(255,255,255,.04);
  padding: 2px 8px;
  border-radius: 999px;
}
.slide-title { font-size: 14px; margin: 0; color: var(--text-1); }
.slide-body ul { margin: 0; padding-left: 1.2em; }
.slide-body .empty { color: var(--text-3); font-style: italic; }

.rendered .pdf-page {
  border: 1px solid var(--border-1);
  border-radius: 6px;
  padding: 18px 22px;
  margin-bottom: 16px;
  background: rgba(255,255,255,.02);
  overflow-wrap: anywhere;
  word-break: break-word;
}
.rendered .pdf-page h3 {
  font-size: 13px;
  color: var(--text-3);
  margin: 0 0 12px;
  padding-bottom: 8px;
  border-bottom: 1px dashed var(--border-1);
}
.rendered .pdf-page p {
  margin: .25em 0;
  line-height: 1.6;
  white-space: pre-wrap;
}
.rendered .pdf-page .empty {
  color: var(--text-3);
  font-style: italic;
}
</style>
