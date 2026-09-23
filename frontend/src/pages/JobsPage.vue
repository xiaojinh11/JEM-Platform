<script setup lang="ts">
import { onMounted, reactive, ref, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'

interface Device { id: number; name: string }
interface JobRow {
  id: number; name: string; kind: string; status: string;
  created_at: string; risk_assessment?: any; rollback_plan?: any;
  ai_comparison?: any; requires_approval?: boolean; approved_by?: string | null;
}
interface DryRunPreview {
  job_kind: string; estimated_minutes: number; notes: string[];
  risk: {
    is_dangerous: boolean; matched_commands: string[]; warning: string;
    requires_approval: boolean; ai_comment: string | null;
    blast_radius: Record<string, { name: string; neighbours: number[] }>;
  }
  commands_by_device: { device_id: number; commands: string[] }[]
}

const list = ref<JobRow[]>([])
const devices = ref<Device[]>([])
const detail = ref<Record<string, unknown> | null>(null)
const loading = ref(false)
const previewing = ref(false)
const preview = ref<DryRunPreview | null>(null)
const previewError = ref<string | null>(null)

const form = reactive({
  name: '',
  kind: 'batch_read',
  target_device_ids: [] as number[],
  batch_size: 5,
  params_json: '{}',
  note: '',
})

const refresh = async () => {
  loading.value = true
  try {
    list.value = (await api.listJobs()) as JobRow[]
    devices.value = (await api.listDevices()) as Device[]
  } finally {
    loading.value = false
  }
}

const parsedParams = computed<Record<string, unknown> | null>(() => {
  if (form.kind === 'batch_read') return null
  try { return JSON.parse(form.params_json || '{}') }
  catch { return null }
})

const dryRun = async () => {
  if (form.kind === 'batch_read') {
    ElMessage.info('只读任务无需 dry-run')
    return
  }
  if (form.target_device_ids.length === 0) {
    ElMessage.warning('请先选择目标设备')
    return
  }
  previewError.value = null
  previewing.value = true
  try {
    const params = parsedParams.value
    if (params === null) {
      ElMessage.error('参数 JSON 格式错误')
      return
    }
    preview.value = await api.dryRunJob({
      commands: form.kind === 'batch_write' ? ['display this'] : [],
      params,
      device_ids: form.target_device_ids,
    })
  } catch (e: any) {
    previewError.value = e?.response?.data?.detail || String(e)
  } finally {
    previewing.value = false
  }
}

const submit = async () => {
  if (!form.name) { ElMessage.warning('请填写任务名称'); return }
  if (!form.target_device_ids.length) { ElMessage.warning('请选择至少一台设备'); return }
  const params = parsedParams.value
  if (form.kind !== 'batch_read' && params === null) {
    ElMessage.error('参数 JSON 格式错误')
    return
  }
  await api.createJob({ ...form, params })
  ElMessage.success('任务已创建')
  refresh()
}

const start = async (id: number) => {
  await ElMessageBox.confirm('确认启动该任务？', '提示', { type: 'warning' })
  await api.startJob(id)
  ElMessage.success('任务已入队')
  refresh()
}

const cancel = async (id: number) => {
  await api.cancelJob(id)
  ElMessage.success('已请求取消')
  refresh()
}

const show = async (id: number) => {
  detail.value = (await api.getJob(id)) as Record<string, unknown>
}

const fetchRollback = async (id: number) => {
  const plan = await api.rollbackPlan(id)
  ElMessageBox.alert(formatJson(plan), '回滚计划', { confirmButtonText: '关闭' })
}

const fetchAiCompare = async (id: number) => {
  const comp = await api.aiCompareJob(id)
  ElMessageBox.alert(formatJson(comp), 'AI 对比报告', { confirmButtonText: '关闭' })
}

const formatJson = (obj: unknown) => {
  try { return JSON.stringify(obj, null, 2) } catch { return String(obj) }
}

const statusTag = (s: string) => {
  if (s === 'succeeded') return 'success'
  if (s === 'failed') return 'danger'
  if (s === 'needs_verification') return 'warning'
  if (s === 'running') return 'primary'
  return 'info'
}

const kindLabel = (k: string) => {
  if (k === 'batch_read') return '批量采集'
  if (k === 'batch_write') return '批量下发'
  if (k === 'template_apply') return '模板应用'
  return k
}

const riskLevel = (row: JobRow): { label: string; tone: string } => {
  const level = row.risk_assessment?.level
  if (level === 'dangerous') return { label: '高危', tone: 'danger' }
  if (row.requires_approval) return { label: '需审批', tone: 'warning' }
  return { label: '安全', tone: 'success' }
}

onMounted(refresh)
</script>

<template>
  <div class="page" v-loading="loading">

    <div class="page-grid">
      <!-- 左侧：任务列表 -->
      <div class="panel">
        <div class="panel-header">
          <span>任务列表</span>
          <el-button size="small" @click="refresh">刷新</el-button>
        </div>
        <el-table :data="list" border stripe size="small">
          <el-table-column type="index" label="#" width="50" align="center" />
          <el-table-column prop="name" label="名称" min-width="130" show-overflow-tooltip />
          <el-table-column label="类型" width="100">
            <template #default="{ row }">
              <el-tag size="small" effect="plain">{{ kindLabel(String((row as JobRow).kind)) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="100" align="center">
            <template #default="{ row }">
              <el-tag size="small" :type="statusTag(String((row as JobRow).status))" effect="dark">
                {{ (row as JobRow).status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="AI 风险" width="80" align="center">
            <template #default="{ row }">
              <el-tag size="small" :type="riskLevel(row as JobRow).tone as any" effect="plain">
                {{ riskLevel(row as JobRow).label }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="created_at" label="创建时间" min-width="140" show-overflow-tooltip />
          <el-table-column label="操作" width="280" align="center">
            <template #default="{ row }">
              <el-button size="small" link @click="show((row as JobRow).id)">详情</el-button>
              <el-button size="small" link type="primary"
                :disabled="!['draft','pending','canceled'].includes(String((row as JobRow).status))"
                @click="start((row as JobRow).id)">启动</el-button>
              <el-button size="small" link @click="fetchRollback((row as JobRow).id)">回滚计划</el-button>
              <el-button size="small" link type="info"
                :disabled="(row as JobRow).status !== 'succeeded'"
                @click="fetchAiCompare((row as JobRow).id)">AI 对比</el-button>
              <el-button size="small" link type="danger" @click="cancel((row as JobRow).id)">取消</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 右侧：创建表单 -->
      <div class="panel">
        <div class="panel-header">
          <span>创建任务</span>
        </div>
        <div class="form-body">
          <div class="form-field">
            <label>任务名称</label>
            <el-input v-model="form.name" placeholder="输入任务名称" />
          </div>
          <div class="form-field">
            <label>任务类型</label>
            <el-select v-model="form.kind" style="width:100%">
              <el-option value="batch_read" label="批量采集（只读）" />
              <el-option value="batch_write" label="批量下发（带备份）" />
              <el-option value="template_apply" label="模板应用" />
            </el-select>
          </div>
          <div class="form-field">
            <label>目标设备</label>
            <el-select v-model="form.target_device_ids" multiple filterable collapse-tags style="width:100%">
              <el-option
                v-for="d in devices" :key="(d as Device).id"
                :value="(d as Device).id"
                :label="(d as Device).name"
              />
            </el-select>
          </div>
          <div class="form-field">
            <label>每批大小</label>
            <el-input-number v-model="form.batch_size" :min="1" :max="50" />
          </div>
          <div class="form-field" v-if="form.kind !== 'batch_read'">
            <label>参数 JSON</label>
            <el-input v-model="form.params_json" type="textarea" :autosize="{minRows:3}" placeholder='{"key":"value"}' />
          </div>
          <div class="form-field">
            <label>备注</label>
            <el-input v-model="form.note" placeholder="可选" />
          </div>
          <div class="btn-row">
            <el-button @click="dryRun" :loading="previewing" :disabled="form.kind === 'batch_read'">
              Dry-Run 预览
            </el-button>
            <el-button type="primary" @click="submit">创建任务</el-button>
          </div>
        </div>

        <!-- Dry-run 预览结果 -->
        <div v-if="preview || previewError" class="preview-box">
          <div class="preview-header">
            <span>Dry-Run 预览</span>
            <el-button size="small" text @click="preview = null; previewError = null">关闭</el-button>
          </div>
          <div v-if="previewError" class="preview-error">{{ previewError }}</div>
          <div v-else-if="preview">
            <div class="preview-row">
              <span>预计耗时</span>
              <b>{{ preview.estimated_minutes }} 分钟</b>
            </div>
            <div class="preview-row">
              <span>高危命令</span>
              <el-tag v-if="preview.risk.is_dangerous" type="danger" size="small">
                {{ preview.risk.matched_commands.length }} 条
              </el-tag>
              <el-tag v-else type="success" size="small">无</el-tag>
            </div>
            <div v-if="preview.risk.ai_comment" class="ai-comment">
              <b>AI 提示：</b>{{ preview.risk.ai_comment }}
            </div>
            <div v-if="preview.notes.length" class="notes">
              <div v-for="(n, i) in preview.notes" :key="i" class="note">• {{ n }}</div>
            </div>
            <details v-if="preview.risk.matched_commands.length">
              <summary>高危命令清单</summary>
              <pre class="cmd-list">{{ preview.risk.matched_commands.join('\n') }}</pre>
            </details>
          </div>
        </div>
      </div>
    </div>

    <!-- 详情 -->
    <div v-if="detail" class="detail-panel">
      <div class="panel-header">
        <span>任务详情 · {{ (detail as {name:string}).name }}</span>
        <el-button size="small" text @click="detail = null">关闭</el-button>
      </div>
      <pre class="detail-json">{{ JSON.stringify(detail, null, 2) }}</pre>
    </div>

  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.page-grid {
  display: grid;
  grid-template-columns: 1fr 380px;
  gap: 16px;
}

.panel {
  background: var(--bg-canvas);
  border-radius: var(--radius-md);
  border: 1px solid var(--border-1);
  display: flex;
  flex-direction: column;
  color: var(--text-1);
}

.panel-header {
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-1);
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-1);
}

.form-body {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.form-field { display: flex; flex-direction: column; gap: 5px; }
.form-field label { font-size: 12px; font-weight: 500; color: var(--text-3); }
.btn-row { display: flex; gap: 8px; margin-top: 4px; }

.preview-box {
  border-top: 1px solid var(--border-1);
  padding: 12px 16px;
  background: var(--bg-code);
}
.preview-header {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 10px; font-size: 12px; font-weight: 600;
}
.preview-row {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 8px; font-size: 12px;
}
.preview-row > span { color: var(--text-3); }
.preview-error { color: var(--el-color-danger); font-size: 12px; }
.ai-comment {
  background: var(--bg-canvas);
  border-left: 3px solid var(--el-color-primary);
  padding: 8px 10px;
  margin: 8px 0;
  font-size: 12px;
  line-height: 1.5;
  border-radius: 4px;
}
.notes { display: flex; flex-direction: column; gap: 3px; margin: 6px 0; }
.note { font-size: 11px; color: var(--text-3); }
.cmd-list {
  margin: 6px 0 0; padding: 8px; font-size: 11px;
  background: var(--bg-canvas); border-radius: 4px;
  max-height: 120px; overflow: auto; font-family: var(--font-mono);
}

.detail-panel {
  background: var(--bg-canvas);
  border-radius: var(--radius-md);
  border: 1px solid var(--border-1);
}
.detail-json {
  margin: 0;
  padding: 16px;
  font-size: 12px;
  line-height: 1.6;
  background: var(--bg-code);
  color: var(--text-1);
  max-height: 400px;
  overflow: auto;
  font-family: var(--font-mono);
  border-radius: var(--radius-md);
}
</style>
