<script setup lang="ts">
import { onMounted, ref, reactive, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api'
import type { AIConfig, AIStats } from '@/api'
import JellyRadioWrapper from '@/components/react/JellyRadioWrapper.vue'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  'update:modelValue': [boolean]
  saved: []
}>()

const open = ref(props.modelValue)
watch(() => props.modelValue, (v) => (open.value = v))
watch(open, (v) => emit('update:modelValue', v))

const stats = ref<AIStats | null>(null)
const testing = ref(false)
const fetchingModels = ref(false)
const saving = ref(false)
const config = reactive<Partial<AIConfig>>({
  provider: 'openai',
  base_url: 'https://api.openai.com/v1',
  api_key: '',
  model: 'gpt-4o-mini',
  temperature: 0.3,
  max_tokens: 2048,
  system_prompt: '',
  enabled: false,
  active_persona: 'chat',
})
const models = ref<string[]>([])
const lastTestResult = ref<{ ok: boolean; message: string } | null>(null)

const providerPresets: Record<string, { url: string; models: string[] }> = {
  openai:    { url: 'https://api.openai.com/v1',          models: ['gpt-4o-mini', 'gpt-4o', 'gpt-4-turbo', 'gpt-3.5-turbo'] },
  deepseek:  { url: 'https://api.deepseek.com/v1',        models: ['deepseek-chat', 'deepseek-reasoner'] },
  qwen:      { url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', models: ['qwen-plus', 'qwen-turbo', 'qwen-max'] },
  moonshot:  { url: 'https://api.moonshot.cn/v1',         models: ['moonshot-v1-8k', 'moonshot-v1-32k', 'moonshot-v1-128k'] },
  zhipu:     { url: 'https://open.bigmodel.cn/api/paas/v4', models: ['glm-4-plus', 'glm-4-air', 'glm-4-flash'] },
  custom:    { url: '', models: [] },
}

const refresh = async () => {
  const [cfg, s] = await Promise.all([api.aiGetConfig(), api.aiStats()])
  Object.assign(config, {
    provider: cfg.provider,
    base_url: cfg.base_url,
    api_key: '',
    model: cfg.model,
    temperature: cfg.temperature,
    max_tokens: cfg.max_tokens,
    system_prompt: cfg.system_prompt,
    enabled: cfg.enabled,
    active_persona: cfg.active_persona,
  })
  stats.value = s
}

const onProviderChange = () => {
  const preset = providerPresets[config.provider || 'openai']
  if (preset && preset.url) {
    config.base_url = preset.url
    if (preset.models.length && !preset.models.includes(config.model || '')) {
      config.model = preset.models[0]
    }
  }
}

const testConnection = async () => {
  testing.value = true
  try {
    // Save first so the server has the latest creds to test
    await api.aiUpdateConfig({ ...config })
    const res = await api.aiTestConfig()
    lastTestResult.value = { ok: res.ok, message: res.message }
    if (res.ok) {
      models.value = res.models || []
      ElMessage.success('连通测试成功')
    } else {
      ElMessage.error('连通失败: ' + res.message)
    }
    await refresh()
  } catch (e) {
    ElMessage.error('测试失败: ' + (e as Error).message)
  } finally {
    testing.value = false
  }
}

const fetchModels = async () => {
  fetchingModels.value = true
  try {
    // Save first so the server has the latest creds to test
    await api.aiUpdateConfig({ ...config })
    const res = await api.aiFetchModels()
    if (res.ok) {
      models.value = res.models || []
      ElMessage.success(res.models.length ? `获取到 ${res.models.length} 个模型` : '未找到可用模型')
    } else {
      ElMessage.error('获取模型失败: ' + res.message)
    }
  } catch (e) {
    ElMessage.error('获取模型失败: ' + (e as Error).message)
  } finally {
    fetchingModels.value = false
  }
}

const save = async () => {
  saving.value = true
  try {
    await api.aiUpdateConfig({ ...config })
    ElMessage.success('配置已保存')
    await refresh()
    emit('saved')
  } catch (e) {
    ElMessage.error('保存失败: ' + (e as Error).message)
  } finally {
    saving.value = false
  }
}

onMounted(refresh)
</script>

<template>
  <el-drawer
    v-model="open"
    direction="rtl"
    size="520px"
    :close-on-click-modal="true"
    :title="null"
  >
    <template #header>
      <div class="ai-head-title">
        <span class="ai-icon">⚙️</span>
        <div>
          <div class="ai-title">AI Agent 配置</div>
          <div class="ai-sub">配置 OpenAI 兼容接口 · 启用/禁用 Agent</div>
        </div>
      </div>
    </template>
    <div class="ai-root">
      <div class="ai-head">
        <div class="ai-head-title-spacer" />
        <el-switch v-model="config.enabled" active-text="已启用" inactive-text="未启用" />
      </div>

      <!-- Stats summary -->
      <div v-if="stats" class="ai-stats">
        <div class="st-card">
          <div class="st-label">对话总数</div>
          <div class="st-value mono">{{ stats.total_chats }}</div>
        </div>
        <div class="st-card">
          <div class="st-label">工具调用</div>
          <div class="st-value mono">{{ stats.tool_calls_total }}</div>
        </div>
        <div class="st-card">
          <div class="st-label">成功率</div>
          <div class="st-value mono">{{ Math.round(stats.tool_success_rate * 100) }}%</div>
        </div>
        <div class="st-card">
          <div class="st-label">Tokens</div>
          <div class="st-value mono">{{ stats.total_tokens }}</div>
        </div>
      </div>

      <div class="ai-section">
        <div class="ai-label">服务提供商</div>
        <el-select v-model="config.provider" @change="onProviderChange" style="width: 100%">
          <el-option v-for="(p, k) in providerPresets" :key="k" :label="k" :value="k" />
        </el-select>
      </div>

      <div class="ai-section">
        <div class="ai-label">API Base URL</div>
        <el-input v-model="config.base_url" placeholder="https://api.openai.com/v1" />
      </div>

      <div class="ai-section">
        <div class="ai-label">
          API Key
          <span v-if="config.api_key === ''" class="ai-hint">(留空保留旧 Key)</span>
        </div>
        <el-input
          v-model="config.api_key"
          type="password"
          show-password
          placeholder="sk-..."
          autocomplete="off"
        />
      </div>

      <div class="ai-section">
        <div class="ai-label">
          模型
          <el-button size="small" text type="primary" :loading="fetchingModels" style="margin-left: 8px; font-size: 10px; padding: 0 4px;" @click="fetchModels">
            {{ fetchingModels ? '获取中…' : '↺ 获取模型列表' }}
          </el-button>
        </div>
        <el-select v-model="config.model" filterable allow-create style="width: 100%">
          <el-option
            v-for="m in (models.length ? models : providerPresets[config.provider || 'openai']?.models || [])"
            :key="m"
            :label="m"
            :value="m"
          />
        </el-select>
      </div>

      <div class="ai-row">
        <div class="ai-section" style="flex: 1">
          <div class="ai-label">Temperature</div>
          <el-input-number v-model="config.temperature" :min="0" :max="2" :step="0.1" style="width: 100%" />
        </div>
        <div class="ai-section" style="flex: 1">
          <div class="ai-label">Max Tokens</div>
          <el-input-number v-model="config.max_tokens" :min="64" :max="32000" :step="256" style="width: 100%" />
        </div>
      </div>

      <div class="ai-section">
        <div class="ai-label">系统提示词</div>
        <el-input
          v-model="config.system_prompt"
          type="textarea"
          :rows="4"
          placeholder="自定义 Agent 的人设..."
        />
      </div>

      <div class="ai-section">
        <div class="ai-label">当前 Persona</div>
        <JellyRadioWrapper
          v-model="config.active_persona"
          :items="[
            { value: 'chat', label: 'AI Chat' },
            { value: 'agent', label: 'AI Agent' },
            { value: 'monitor', label: 'AI Monitor' },
          ]"
          aria-label="AI Persona"
        />
      </div>

      <!-- Last test -->
      <div v-if="lastTestResult" class="ai-test-result" :class="{ ok: lastTestResult.ok }">
        <span class="dot" />
        <span>{{ lastTestResult.message }}</span>
      </div>

      <div class="ai-actions">
        <el-button @click="testConnection" :loading="testing">连通测试</el-button>
        <el-button type="primary" @click="save" :loading="saving">保存配置</el-button>
      </div>
    </div>
  </el-drawer>
</template>

<style scoped>
.ai-root {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 0 0 20px;
  height: 100%;
  overflow-y: auto;
}
.ai-head {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  padding: 0 20px 12px;
}
.ai-head-title { display: flex; align-items: center; gap: 10px; }
.ai-head-title-spacer { flex: 1; }
.ai-icon { font-size: 20px; }
.ai-title { font-size: 15px; font-weight: 700; color: var(--text-1); }
.ai-sub { font-size: 11px; color: var(--text-3); margin-top: 2px; }

.ai-stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin: 4px 20px 0;
}
.st-card {
  background: var(--bg-elev);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  padding: 10px;
  text-align: center;
}
.st-label { font-size: 10px; color: var(--text-3); margin-bottom: 4px; }
.st-value { font-size: 18px; font-weight: 700; color: var(--text-1); }

.ai-section {
  padding: 0 20px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.ai-label {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.6px;
  color: var(--text-3);
  text-transform: uppercase;
}
.ai-hint { font-weight: 400; text-transform: none; font-size: 10.5px; opacity: 0.7; }
.ai-row { display: flex; gap: 10px; padding: 0 20px; }

.ai-test-result {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  margin: 0 20px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.3);
  color: #ef4444;
}
.ai-test-result.ok {
  background: rgba(34, 197, 94, 0.1);
  border-color: rgba(34, 197, 94, 0.3);
  color: #22c55e;
}
.ai-test-result .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
}

.ai-actions {
  display: flex;
  gap: 10px;
  justify-content: flex-end;
  padding: 10px 20px 0;
  border-top: 1px solid var(--border-1);
  margin-top: auto;
}
</style>
