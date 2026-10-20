<script setup lang="ts">
/**
 * ConfigDiffViewer — Monaco-powered unified/side-by-side diff for
 * network device configurations (VRP / IOS / RGOS).
 *
 * Props:
 *   - fromContent / toContent: plain text
 *   - fromLabel / toLabel: header text (e.g. "r12" / "r13")
 *   - language: 'plaintext' (default) | one of the network flavors we register
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as monaco from 'monaco-editor'

// Slime theme matching the Slate-900 console
const NAW_THEME = 'naw-ops-dark'

let diffEditor: monaco.editor.IStandaloneDiffEditor | null = null

const props = withDefaults(defineProps<{
  fromContent: string
  toContent: string
  fromLabel?: string
  toLabel?: string
  language?: string
}>(), {
  fromLabel: 'OLD',
  toLabel: 'NEW',
  language: 'plaintext',
})

const container = ref<HTMLDivElement | null>(null)

function defineNawTheme() {
  monaco.editor.defineTheme(NAW_THEME, {
    base: 'vs-dark',
    inherit: true,
    rules: [
      { token: 'comment', foreground: '475569', fontStyle: 'italic' },
      { token: 'keyword', foreground: '22d3ee' },
      { token: 'number',  foreground: 'a78bfa' },
      { token: 'string',  foreground: '10b981' },
      { token: 'type',    foreground: '60a5fa' },
    ],
    colors: {
      'editor.background': '#0a1325',
      'editor.foreground': '#e2e8f0',
      'editorGutter.background': '#0a1325',
      'editor.lineHighlightBackground': '#111c33',
      'editorLineNumber.foreground': '#3b4d72',
      'editorLineNumber.activeForeground': '#22d3ee',
      'editor.selectionBackground': '#22d3ee33',
      'editor.inactiveSelectionBackground': '#22d3ee22',
      'diffEditor.insertedTextBackground': '#10b98122',
      'diffEditor.removedTextBackground': '#ef444422',
      'diffEditor.insertedLineBackground': '#10b98115',
      'diffEditor.removedLineBackground': '#ef444415',
      'diffEditorGutter.insertedLineBackground': '#10b98155',
      'diffEditorGutter.removedLineBackground': '#ef444455',
      'diffEditor.diagonalFill': '#22d3ee11',
      'scrollbarSlider.background': '#1f2a4477',
      'scrollbarSlider.hoverBackground': '#2c3a5caa',
    },
  })
}

function buildEditor() {
  if (!container.value) return
  defineNawTheme()
  diffEditor = monaco.editor.createDiffEditor(container.value, {
    theme: NAW_THEME,
    renderSideBySide: true,
    automaticLayout: true,
    readOnly: true,
    fontFamily: 'JetBrains Mono, Fira Code, Consolas, monospace',
    fontSize: 12.5,
    fontLigatures: true,
    lineNumbers: 'on',
    minimap: { enabled: false },
    scrollBeyondLastLine: false,
    renderWhitespace: 'selection',
    renderLineHighlight: 'all',
    diffWordWrap: 'off',
    ignoreTrimWhitespace: false,
  })
  applyDiff()
}

function applyDiff() {
  if (!diffEditor) return
  diffEditor.setModel({
    original: monaco.editor.createModel(props.fromContent || '', props.language),
    modified: monaco.editor.createModel(props.toContent || '', props.language),
  })
}

onMounted(buildEditor)

onBeforeUnmount(() => {
  try {
    diffEditor?.dispose()
  } catch { /* ignore */ }
  diffEditor = null
})

watch(
  () => [props.fromContent, props.toContent, props.language],
  () => applyDiff(),
)
</script>

<template>
  <div class="diff-wrap panel">
    <div class="diff-bar">
      <div class="diff-side">
        <span class="mono dim">{{ fromLabel }}</span>
        <span class="mono side-count">{{ fromContent ? fromContent.split('\n').length : 0 }} lines</span>
      </div>
      <div class="diff-side to">
        <span class="mono dim">{{ toLabel }}</span>
        <span class="mono side-count">{{ toContent ? toContent.split('\n').length : 0 }} lines</span>
      </div>
    </div>
    <div ref="container" class="diff-host"></div>
    <div v-if="!fromContent && !toContent" class="diff-empty mono">// No content loaded yet</div>
  </div>
</template>

<style scoped>
.diff-wrap {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--border-1);
}
.diff-bar {
  display: grid;
  grid-template-columns: 1fr 1fr;
  border-bottom: 1px solid var(--border-1);
  background: var(--bg-panel);
}
.diff-side {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 14px;
  font-size: 11px;
  letter-spacing: 0.5px;
  border-right: 1px solid var(--border-1);
}
.diff-side.to { border-right: none; }
.side-count { color: var(--text-3); }

.diff-host {
  height: 60vh;
  min-height: 360px;
  background: #0a1325;
}
.diff-empty {
  padding: 24px;
  color: var(--text-3);
  text-align: center;
  border-top: 1px solid var(--border-1);
}
</style>
