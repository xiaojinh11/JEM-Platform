<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { ArrowDown, Close, Document, Paperclip, Promotion, Search, Setting, UploadFilled } from '@element-plus/icons-vue'

export interface PromptAttachment {
  type: 'image' | 'file' | 'url'
  name: string
  url?: string
  text?: string
  value?: string
  size?: number
}

interface PromptSource {
  key: string
  name: string
  description?: string
  attach?: boolean
}

interface PromptCommand {
  key: string
  name: string
  description?: string
}

interface PromptModel {
  key: string
  name: string
  tag?: string
}

const props = withDefaults(defineProps<{
  placeholder?: string
  sources?: PromptSource[]
  commands?: PromptCommand[]
  attachments?: PromptAttachment[]
  models?: PromptModel[]
  defaultModel?: string
  efforts?: string[]
  defaultEffort?: string
  busy?: boolean
}>(), {
  placeholder: '输入问题（Enter 发送，Shift+Enter 换行）',
  sources: () => [],
  commands: () => [],
  attachments: () => [],
  models: () => [],
  defaultModel: '',
  efforts: () => ['低', '中', '高', '极高', '最大'],
  defaultEffort: '',
  busy: false,
})

const emit = defineEmits<{
  send: [payload: { text: string; attachments: PromptAttachment[]; model?: PromptModel; effort?: string }]
  stop: []
  attach: []
  effortChange: [effort: string]
  removeAttachment: [index: number]
  clearAttachments: []
}>()

const draft = ref('')
const input = ref<HTMLTextAreaElement | null>(null)
const menu = ref<'source' | 'command' | 'model' | 'effort' | null>(null)
const selectedIndex = ref(0)
const selectedModelKey = ref(props.defaultModel || props.models[0]?.key || '')
const effortIndex = ref(
  props.defaultEffort
    ? Math.max(0, props.efforts.indexOf(props.defaultEffort))
    : Math.max(0, Math.floor((props.efforts.length - 1) / 2)),
)

const selectedModel = computed(() => props.models.find((model) => model.key === selectedModelKey.value) || props.models[0])
const token = computed(() => {
  const match = /(^|\s)([@/])([\w-]*)$/.exec(draft.value)
  if (!match) return null
  return { kind: match[2] === '@' ? 'source' : 'command', query: match[3].toLowerCase(), start: match.index + match[1].length }
})
const activeMenu = computed(() => menu.value || token.value?.kind || null)
const query = computed(() => menu.value ? '' : token.value?.query || '')
const visibleItems = computed(() => {
  if (activeMenu.value === 'source') {
    return props.sources.filter((item) => item.name.toLowerCase().includes(query.value))
  }
  if (activeMenu.value === 'command') {
    return props.commands.filter((item) => item.name.replace(/^\//, '').toLowerCase().startsWith(query.value))
  }
  if (activeMenu.value === 'model') return props.models
  return []
})
const effort = computed(() => props.efforts[effortIndex.value] || '')
const canSend = computed(() => draft.value.trim().length > 0 || props.attachments.length > 0)
const armed = computed(() => props.busy || canSend.value)

watch(() => props.defaultModel, (value) => {
  if (value) selectedModelKey.value = value
})
watch(() => props.models, (models) => {
  if (!models.some((model) => model.key === selectedModelKey.value)) {
    selectedModelKey.value = models[0]?.key || ''
  }
}, { deep: true })
watch(() => props.efforts, (efforts) => {
  effortIndex.value = Math.min(effortIndex.value, Math.max(0, efforts.length - 1))
})
watch(draft, () => nextTick(resizeInput))

function resizeInput() {
  if (!input.value) return
  input.value.style.height = '0px'
  input.value.style.height = `${Math.min(input.value.scrollHeight, 132)}px`
  input.value.style.overflowY = input.value.scrollHeight > 132 ? 'auto' : 'hidden'
}

function focusInput() {
  input.value?.focus({ preventScroll: true })
}

function closeMenus() {
  menu.value = null
  selectedIndex.value = 0
}

function openMenu(kind: 'source' | 'model' | 'effort') {
  menu.value = kind
  selectedIndex.value = kind === 'model' ? Math.max(0, props.models.findIndex((model) => model.key === selectedModelKey.value)) : 0
  focusInput()
}

function pickItem(item: PromptSource | PromptCommand | PromptModel) {
  if (activeMenu.value === 'model') {
    selectedModelKey.value = (item as PromptModel).key
    closeMenus()
    focusInput()
    return
  }
  const currentToken = token.value
  const head = currentToken ? draft.value.slice(0, currentToken.start) : draft.value
  if (activeMenu.value === 'source') {
    const source = item as PromptSource
    if (source.attach) {
      draft.value = head
      emit('attach')
    } else {
      draft.value = `${head}@${source.key} `
    }
  } else {
    draft.value = `${head}${(item as PromptCommand).name} `
  }
  closeMenus()
  focusInput()
}

function send() {
  if (!canSend.value || props.busy) return
  emit('send', {
    text: draft.value.trim(),
    attachments: [...props.attachments],
    model: selectedModel.value,
    effort: effort.value,
  })
  draft.value = ''
  closeMenus()
  focusInput()
}

function onKeydown(event: KeyboardEvent) {
  if (activeMenu.value && visibleItems.value.length) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      const delta = event.key === 'ArrowDown' ? 1 : -1
      selectedIndex.value = (selectedIndex.value + delta + visibleItems.value.length) % visibleItems.value.length
      return
    }
    if ((event.key === 'Enter' && !event.shiftKey) || event.key === 'Tab') {
      event.preventDefault()
      pickItem(visibleItems.value[selectedIndex.value])
      return
    }
  }
  if (event.key === 'Escape') {
    if (activeMenu.value) {
      event.preventDefault()
      closeMenus()
    }
    return
  }
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    send()
  }
}

function setEffort(index: number) {
  effortIndex.value = Math.max(0, Math.min(props.efforts.length - 1, index))
  emit('effortChange', effort.value)
}

function removeAttachment(index: number) {
  emit('removeAttachment', index)
}

function clearAttachments() {
  if (!props.attachments.length) return
  emit('clearAttachments')
}
</script>

<template>
  <div class="prompt-bar" :data-busy="busy || undefined" :data-max="effortIndex === efforts.length - 1 || undefined">
    <div v-if="activeMenu" class="prompt-menu" :data-kind="activeMenu">
      <template v-if="activeMenu === 'effort'">
        <div class="prompt-menu-head"><span>思考强度</span><b>{{ effort }}</b></div>
        <div class="effort-labels"><span>更快</span><span>更深</span></div>
        <input
          class="effort-range"
          type="range"
          :min="0"
          :max="Math.max(0, efforts.length - 1)"
          :value="effortIndex"
          aria-label="思考强度"
          @input="setEffort(Number(($event.target as HTMLInputElement).value))"
        >
      </template>
      <template v-else>
        <button
          v-for="(item, index) in visibleItems"
          :key="item.key"
          type="button"
          class="prompt-option"
          :class="{ active: index === selectedIndex }"
          @mouseenter="selectedIndex = index"
          @mousedown.prevent
          @click="pickItem(item)"
        >
          <el-icon v-if="activeMenu === 'source'"><Paperclip v-if="(item as PromptSource).attach" /><Promotion v-else /></el-icon>
          <span class="option-name">{{ item.name }}</span>
          <span v-if="item.description" class="option-description">{{ item.description }}</span>
          <small v-if="activeMenu === 'model' && (item as PromptModel).tag">{{ (item as PromptModel).tag }}</small>
        </button>
        <div v-if="!visibleItems.length" class="prompt-empty">没有匹配项</div>
      </template>
    </div>

    <div class="prompt-surface" @click="focusInput">
<div v-if="attachments.length" class="attachment-bar">
      <div class="attachment-chips">
        <span v-for="(attachment, index) in attachments" :key="`${attachment.name}-${index}`" class="attachment-chip">
          <el-icon><Document /></el-icon>
          <span>{{ attachment.name }}</span>
          <button type="button" title="移除附件" @click.stop="removeAttachment(index)"><Close /></button>
        </span>
      </div>
      <button type="button" class="attachment-clear" title="清空所有附件" @click.stop="clearAttachments">
        清空
      </button>
    </div>
      <textarea
        ref="input"
        v-model="draft"
        rows="1"
        :placeholder="busy ? 'AI 正在处理…' : placeholder"
        aria-label="AI 对话输入"
        @keydown="onKeydown"
      />
      <div class="prompt-toolbar" @click.stop>
        <button type="button" class="prompt-tool" title="添加文件或数据源" @click="openMenu('source')"><el-icon><UploadFilled /></el-icon></button>
        <button v-if="models.length" type="button" class="prompt-picker" title="选择模型" @click="openMenu('model')">
          {{ selectedModel?.name || '当前模型' }} <el-icon><ArrowDown /></el-icon>
        </button>
        <button v-if="efforts.length" type="button" class="prompt-picker" title="选择思考强度" :class="{ maxed: effortIndex === efforts.length - 1 }" @click="openMenu('effort')">
          ✦ {{ effort }}
        </button>
        <span class="prompt-spacer" />
        <button
          type="button"
          class="prompt-send"
          :class="{ armed, busy }"
          :disabled="!armed"
          :title="busy ? '停止生成' : '发送'"
          @click="busy ? emit('stop') : send()"
        >
          <span v-if="busy" class="stop-glyph" />
          <span v-else>↑</span>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.prompt-bar { position: relative; width: 100%; color: var(--text-1); font-size: 14px; }
.prompt-menu { position: absolute; right: 0; bottom: calc(100% + 8px); left: 0; z-index: 4; max-height: 260px; overflow-y: auto; padding: 5px; border: 1px solid var(--border-2); border-radius: 10px; background: var(--bg-canvas); box-shadow: 0 16px 36px rgba(15, 23, 42, .22); }
.prompt-menu[data-kind='model'], .prompt-menu[data-kind='effort'] { right: auto; left: 84px; width: 240px; }
.prompt-option { display: flex; align-items: center; gap: 8px; width: 100%; min-height: 36px; padding: 6px 9px; border: 0; border-radius: 7px; background: transparent; color: var(--text-1); text-align: left; cursor: pointer; }
.prompt-option:hover, .prompt-option.active { background: var(--accent-soft); }
.prompt-option .el-icon { color: var(--accent); flex: 0 0 auto; }
.option-name { flex: 0 0 auto; font-size: 13px; font-weight: 600; }
.option-description { min-width: 0; overflow: hidden; color: var(--text-3); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.prompt-option small { margin-left: auto; color: var(--text-3); }
.prompt-empty { padding: 10px; color: var(--text-3); font-size: 12px; text-align: center; }
.prompt-menu-head, .effort-labels { display: flex; justify-content: space-between; align-items: center; color: var(--text-2); font-size: 12px; }
.prompt-menu-head b { color: var(--accent); }
.effort-labels { margin-top: 12px; color: var(--text-3); font-size: 11px; }
.effort-range { width: 100%; margin: 10px 0 2px; accent-color: var(--accent); }
.prompt-surface { display: flex; flex-direction: column; gap: 8px; padding: 11px 12px 9px; border: 1px solid var(--border-2); border-radius: 14px; background: var(--bg-canvas); box-shadow: 0 5px 18px rgba(15, 23, 42, .08); }
.prompt-surface:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.attachment-bar { display: flex; align-items: flex-start; gap: 8px; }
.attachment-chips { display: flex; flex-wrap: wrap; gap: 6px; flex: 1; min-width: 0; }
.attachment-clear { flex: 0 0 auto; padding: 4px 8px; border: 0; border-radius: 6px; background: transparent; color: var(--text-3); font-size: 11px; cursor: pointer; }
.attachment-clear:hover { background: var(--accent-soft); color: var(--danger); }
.attachment-chip { display: inline-flex; align-items: center; gap: 5px; max-width: 240px; padding: 4px 6px; border: 1px solid var(--border-1); border-radius: 7px; background: var(--bg-elev); color: var(--text-2); font-size: 11px; }
.attachment-chip > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.attachment-chip .el-icon { color: var(--accent); }
.attachment-chip button { display: inline-grid; place-items: center; padding: 0; border: 0; background: transparent; color: var(--text-3); cursor: pointer; }
.attachment-chip button:hover { color: var(--danger); }
textarea { display: block; width: 100%; min-height: 24px; max-height: 132px; padding: 0; border: 0; outline: 0; resize: none; overflow-y: hidden; background: transparent; color: var(--text-1); font: inherit; line-height: 22px; }
textarea::placeholder { color: var(--text-3); }
.prompt-toolbar { display: flex; align-items: center; gap: 4px; }
.prompt-tool, .prompt-picker, .prompt-send { border: 0; cursor: pointer; }
.prompt-tool { display: inline-grid; place-items: center; width: 28px; height: 28px; border-radius: 7px; background: transparent; color: var(--text-3); }
.prompt-tool:hover, .prompt-picker:hover { background: var(--accent-soft); color: var(--accent); }
.prompt-picker { display: inline-flex; align-items: center; gap: 4px; min-height: 28px; padding: 0 8px; border-radius: 7px; background: transparent; color: var(--text-2); font-size: 11px; }
.prompt-picker.maxed { color: var(--accent); }
.prompt-spacer { flex: 1; }
.prompt-send { display: inline-grid; place-items: center; width: 30px; height: 30px; border-radius: 8px; background: var(--bg-elev); color: var(--text-3); font-size: 18px; line-height: 1; }
.prompt-send.armed { background: var(--accent); color: #fff; }
.prompt-send:disabled { cursor: default; opacity: .65; }
.stop-glyph { width: 10px; height: 10px; border-radius: 2px; background: currentColor; }
</style>
