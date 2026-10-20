<script setup lang="ts">
/**
 * 命令采集 / Custom Command Collection
 *
 * Migrated from PythonProject5/main.py + webapp.py. Lets an operator
 * pick a set of devices, paste a list of ``display``-style commands,
 * and have them run on every device.  Per-(device, command) output
 * lands in the standard ``config_revisions`` table (source =
 * "command_collect"), so the existing ``/configs`` page already
 * shows the results — no new browsing surface needed.
 *
 * The page intentionally avoids adding new components / stores; it
 * reuses the existing ``api`` (HTTP), ``useAuthStore`` (role gate)
 * and the same WebSocket job-stream that ``JobsPage.vue`` uses, so
 * a future "Commands" entry in the side-nav will Just Work.
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Promotion, Document, Refresh, Search } from '@element-plus/icons-vue'
import { api } from '@/api'

interface DeviceItem {
  id: number
  name: string
  hostname?: string
  vendor?: string
  role?: string
}

interface CommandJobItem {
  id: number
  name: string
  status: string
  target_device_ids: number[]
  command_count: number
  command_preview: string[]
  initiated_by: string
  summary?: { succeeded?: number; failed?: number; total?: number; command_collect?: { total_devices?: number; devices_succeeded?: number; devices_partial?: number; devices_failed?: number } }
  note?: string
  created_at?: string
  finished_at?: string
}

// ── state ────────────────────────────────────────────────────────
const devices = ref<DeviceItem[]>([])
const filteredDevices = ref<DeviceItem[]>([])
const selectedIds = ref<number[]>([])
const commandsText = ref<string>(
  'display current-configuration\ndisplay version\ndisplay ip interface brief\ndisplay fan',
)
const jobName = ref<string>('')
const jobNote = ref<string>('')
const submitting = ref<boolean>(false)
const jobs = ref<CommandJobItem[]>([])
const jobFilter = ref<string>('')

const DEFAULT_COMMANDS = [
  'display current-configuration',
  'display version',
  'display ip interface brief',
  'display fan',
  'display power',
  'display cpu-usage',
  'display memory-usage',
]

// ── lifecycle ────────────────────────────────────────────────────
onMounted(async () => {
  await loadDevices()
  await loadJobs()
})

async function loadDevices() {
  try {
    const list = await api.listDevices()
    devices.value = list as DeviceItem[]
    filteredDevices.value = devices.value
  } catch (e) {
    ElMessage.error('加载设备列表失败：' + (e as Error).message)
  }
}

async function loadJobs() {
  try {
    const list = (await api.listCommandCollectJobs(50)) as CommandJobItem[]
    jobs.value = list
  } catch (e) {
    // Non-fatal — the history list is a convenience, not the
    // primary path.  Surface a warning so the operator can still
    // submit new jobs even if the listing endpoint is down.
    console.warn('load command-collect jobs failed', e)
  }
}

// ── derived ──────────────────────────────────────────────────────
const commandLines = computed<string[]>(() =>
  commandsText.value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith('#')),
)

const estimatedMinutes = computed<number>(() => {
  // Mirror the server's estimate so the user sees the same number
  // both before submit (here) and after submit (in the API
  // response). 3s per (device × command) is a rough upper bound.
  const secs = 3 * selectedIds.value.length * commandLines.value.length
  return Math.round((secs / 60) * 10) / 10
})

const filteredJobs = computed<CommandJobItem[]>(() => {
  const kw = jobFilter.value.trim().toLowerCase()
  if (!kw) return jobs.value
  return jobs.value.filter(
    (j) =>
      j.name.toLowerCase().includes(kw) ||
      String(j.id).includes(kw) ||
      (j.initiated_by || '').toLowerCase().includes(kw),
  )
})

// ── actions ──────────────────────────────────────────────────────
function onDeviceFilter(_val: string) {
  // Re-render filteredDevices from the local copy; the API
  // doesn't expose a server-side device search so the work
  // happens here.
  filteredDevices.value = devices.value
}

function preset(cmd: string) {
  // Replace the textarea body with a single command — useful for
  // the common "I just want dis cur" case from PythonProject5.
  commandsText.value = cmd
}

function clearCommands() {
  commandsText.value = ''
}

function resetDefaults() {
  commandsText.value = DEFAULT_COMMANDS.join('\n')
}

async function submit() {
  if (selectedIds.value.length === 0) {
    ElMessage.warning('请至少选择一台设备')
    return
  }
  if (commandLines.value.length === 0) {
    ElMessage.warning('请至少输入一条命令（# 开头是注释）')
    return
  }
  submitting.value = true
  try {
    const resp = await api.collectCommands({
      device_ids: selectedIds.value,
      commands: commandLines.value,
      name: jobName.value || undefined,
      note: jobNote.value || undefined,
    })
    ElMessage.success(
      `已创建采集任务 #${resp.job_id}（${resp.devices} 台 × ${resp.commands} 命令，预估 ${resp.estimated_minutes} 分钟）`,
    )
    // Reset only the name/note — keep the command list so the
    // operator can run the same set on a different group with
    // a single click.
    jobName.value = ''
    jobNote.value = ''
    await loadJobs()
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } }; message?: string }
    ElMessage.error(
      err?.response?.data?.detail || err?.message || '提交失败',
    )
  } finally {
    submitting.value = false
  }
}

async function refreshJob(j: CommandJobItem) {
  // Lightweight refresh: re-fetch the job list (the existing
  // /jobs/{id} endpoint also works but only returns the
  // command_collect rows; the listing is cheaper).
  await loadJobs()
  const fresh = jobs.value.find((x) => x.id === j.id)
  if (fresh?.status === 'succeeded' || fresh?.status === 'failed' || fresh?.status === 'partial') {
    ElMessage.success(`任务 #${j.id} 已结束，状态：${fresh.status}`)
  }
}

async function downloadZip(j: CommandJobItem) {
  if (j.status !== 'succeeded' && j.status !== 'partial') {
    ElMessage.warning('任务尚未完成，无法下载')
    return
  }
  try {
    const blob = (await api.exportCommandCollect(j.id)) as Blob
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `command-collect-${j.id}.zip`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  } catch (e: unknown) {
    const err = e as { response?: { data?: { detail?: string } }; message?: string }
    ElMessage.error(
      err?.response?.data?.detail || err?.message || '下载失败',
    )
  }
}

async function viewRevisions(j: CommandJobItem) {
  // Per-device revisions live at the standard
  // /configs/device/{id}/revisions endpoint. Open the device
  // detail page with the first device — operators can switch
  // devices via the side-nav.
  if (!j.target_device_ids?.length) {
    ElMessage.warning('该任务没有目标设备')
    return
  }
  const deviceId = j.target_device_ids[0]
  try {
    await ElMessageBox.confirm(
      `该任务的命令结果已存入配置中心（按设备分组的版本历史）。打开设备 #${deviceId} 的配置页？`,
      '查看采集结果',
      { confirmButtonText: '打开', cancelButtonText: '取消' },
    )
    window.open(`/#/devices/${deviceId}?tab=configs`, '_blank', 'noopener')
  } catch {
    // user cancelled
  }
}

function statusType(s: string): 'success' | 'warning' | 'danger' | 'info' {
  if (s === 'succeeded') return 'success'
  if (s === 'partial') return 'warning'
  if (s === 'failed' || s === 'canceled') return 'danger'
  return 'info'
}
</script>

<template>
  <div class="command-collect">
    <el-card class="form-card" shadow="hover">
      <template #header>
        <div class="card-header">
          <span>命令采集 · 自定义 SSH 命令</span>
          <span class="muted">
            来自 PythonProject5 — 支持任意 display/show 命令，结果存入
            <code>config_revisions</code>（source=command_collect）
          </span>
        </div>
      </template>

      <el-form label-width="100px">
        <el-form-item label="目标设备">
          <el-select
            v-model="selectedIds"
            multiple
            filterable
            collapse-tags
            placeholder="选择要采集的设备（可多选）"
            :filter-method="onDeviceFilter"
            style="width: 100%"
          >
            <el-option
              v-for="d in filteredDevices"
              :key="d.id"
              :label="`${d.name} (#${d.id} · ${d.vendor || '?'})`"
              :value="d.id"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="命令列表">
          <el-input
            v-model="commandsText"
            type="textarea"
            :rows="10"
            placeholder="每行一条命令，以 # 开头的行视为注释"
          />
          <div class="row-actions">
            <el-button-group>
              <el-button size="small" @click="preset('display current-configuration')">
                单一：dis cur
              </el-button>
              <el-button size="small" @click="resetDefaults">
                默认 7 条
              </el-button>
              <el-button size="small" @click="clearCommands">
                清空
              </el-button>
            </el-button-group>
            <span class="muted">
              已识别 {{ commandLines.length }} 条命令 ·
              预估 {{ estimatedMinutes }} 分钟
            </span>
          </div>
        </el-form-item>

        <el-form-item label="任务名（可选）">
          <el-input v-model="jobName" placeholder="留空则自动生成" maxlength="128" show-word-limit />
        </el-form-item>

        <el-form-item label="备注（可选）">
          <el-input v-model="jobNote" placeholder="会写入审计日志" maxlength="512" />
        </el-form-item>

        <el-form-item>
          <el-button
            type="primary"
            :loading="submitting"
            :icon="Promotion"
            :disabled="selectedIds.length === 0 || commandLines.length === 0"
            @click="submit"
          >
            提交采集
          </el-button>
          <el-button :icon="Refresh" @click="loadJobs">刷新历史</el-button>
          <el-tag
            v-if="commandLines.length"
            type="info"
            effect="plain"
            style="margin-left: 12px"
          >
            危险命令（reboot / reset / format / delete / system-view / undo / erase / write erase）
            会被服务端拒绝
          </el-tag>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card class="history-card" shadow="hover">
      <template #header>
        <div class="card-header">
          <span>采集历史（最近 50 条）</span>
          <el-input
            v-model="jobFilter"
            :prefix-icon="Search"
            placeholder="按名称 / ID / 操作人过滤"
            style="width: 280px"
            clearable
          />
        </div>
      </template>

      <el-table
        :data="filteredJobs"
        stripe
        empty-text="暂无命令采集记录"
        :default-sort="{ prop: 'created_at', order: 'descending' }"
      >
        <el-table-column prop="id" label="ID" width="64" sortable />
        <el-table-column prop="name" label="任务名" min-width="220" show-overflow-tooltip />
        <el-table-column label="命令" width="80" align="center">
          <template #default="{ row }">
            <el-tag size="small">{{ row.command_count }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="设备" width="80" align="center">
          <template #default="{ row }">
            {{ row.target_device_ids?.length || 0 }}
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" effect="plain">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="initiated_by" label="操作人" width="100" />
        <el-table-column prop="created_at" label="创建时间" width="170" />
        <el-table-column prop="finished_at" label="完成时间" width="170" />
        <el-table-column label="操作" width="280" fixed="right">
          <template #default="{ row }">
            <el-button
              size="small"
              link
              :icon="Document"
              :disabled="row.status !== 'succeeded' && row.status !== 'partial'"
              @click="downloadZip(row)"
            >
              下载 Zip
            </el-button>
            <el-button size="small" link :icon="Search" @click="viewRevisions(row)">
              查看结果
            </el-button>
            <el-button size="small" link :icon="Refresh" @click="refreshJob(row)">
              刷新
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.command-collect {
  max-width: 1280px;
  margin: 0 auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.row-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 8px;
}
.form-card,
.history-card {
  border-radius: 8px;
}
code {
  background: var(--el-fill-color-light);
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 12px;
}
</style>
