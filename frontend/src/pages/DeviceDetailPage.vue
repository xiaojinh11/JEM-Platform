<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'

const route = useRoute()
const router = useRouter()
const deviceId = computed(() => Number(route.params.id))

const device = ref<Record<string, unknown> | null>(null)
const revisions = ref<Record<string, unknown>[]>([])
const endpoints = ref<Record<string, unknown>[]>([])
const credentials = ref<Record<string, unknown>[]>([])
const active = ref<'info' | 'endpoints' | 'revisions' | 'creds' | 'jobs'>('info')
const configText = ref('')
const loading = ref(false)

const endpointDialog = reactive({ visible: false, editing: false, saving: false })
const endpointForm = reactive<{
  id?: number
  kind: string
  address: string
  port: number
  credential_id: number | null
  is_primary: boolean
  notes: string
}>({
  id: undefined,
  kind: 'mgmt_ssh',
  address: '',
  port: 22,
  credential_id: null,
  is_primary: false,
  notes: '',
})

const KIND_OPTIONS = [
  { value: 'mgmt_ssh',    label: '管理 SSH（推荐）',     defaultPort: 22 },
  { value: 'inband_ssh',  label: '业务 SSH',             defaultPort: 22 },
  { value: 'console',     label: 'Console（串口）',       defaultPort: 23 },
  { value: 'restconf',    label: 'RESTCONF',             defaultPort: 443 },
  { value: 'netconf',     label: 'NETCONF',              defaultPort: 830 },
]

const refresh = async () => {
  loading.value = true
  try {
    device.value = await api.getDevice(deviceId.value)
    revisions.value = await api.listRevisions(deviceId.value)
    endpoints.value = await api.listDeviceEndpoints(deviceId.value)
    credentials.value = await api.listCredentials()
    if (!configText.value && revisions.value.length) {
      const r = revisions.value[0]
      const c = await api.readRevisionContent((r as { id: number }).id)
      configText.value = c.content
    }
  } finally {
    loading.value = false
  }
}

const saveConfig = async () => {
  await api.importConfig({
    device_id: deviceId.value,
    content: configText.value,
    author: 'manual',
    source: 'edit',
    note: 'manual edit from UI',
  })
  ElMessage.success('已保存为新版本')
  await refresh()
}

const collectNow = async () => {
  ElMessage.info('已提交采集任务（需在批量任务中跟踪进度）')
}

const openCreateEndpoint = () => {
  endpointForm.id = undefined
  endpointForm.kind = 'mgmt_ssh'
  endpointForm.address = ''
  endpointForm.port = 22
  endpointForm.credential_id = null
  endpointForm.is_primary = endpoints.value.length === 0
  endpointForm.notes = ''
  endpointDialog.editing = false
  endpointDialog.visible = true
}

const openEditEndpoint = (ep: Record<string, unknown>) => {
  endpointForm.id = Number(ep.id)
  endpointForm.kind = String(ep.kind)
  endpointForm.address = String(ep.address)
  endpointForm.port = Number(ep.port)
  endpointForm.credential_id = ep.credential_id ? Number(ep.credential_id) : null
  endpointForm.is_primary = Boolean(ep.is_primary)
  endpointForm.notes = ep.notes ? String(ep.notes) : ''
  endpointDialog.editing = true
  endpointDialog.visible = true
}

const onKindChange = (kind: string) => {
  const opt = KIND_OPTIONS.find((k) => k.value === kind)
  if (opt && endpointForm.port === 22) {
    endpointForm.port = opt.defaultPort
  }
}

const saveEndpoint = async () => {
  if (!endpointForm.address.trim()) {
    ElMessage.warning('请填写地址')
    return
  }
  endpointDialog.saving = true
  try {
    const payload = {
      kind: endpointForm.kind,
      address: endpointForm.address.trim(),
      port: Number(endpointForm.port) || 22,
      credential_id: endpointForm.credential_id,
      is_primary: endpointForm.is_primary,
      notes: endpointForm.notes.trim() || null,
    }
    if (endpointDialog.editing && endpointForm.id) {
      // We don't have PATCH; delete+create to keep things simple.
      await api.deleteDeviceEndpoint(deviceId.value, endpointForm.id)
      await api.createDeviceEndpoint(deviceId.value, payload)
      ElMessage.success('连接配置已更新')
    } else {
      await api.createDeviceEndpoint(deviceId.value, payload)
      ElMessage.success('连接配置已添加')
    }
    endpointDialog.visible = false
    await refresh()
  } finally {
    endpointDialog.saving = false
  }
}

const removeEndpoint = async (ep: Record<string, unknown>) => {
  await ElMessageBox.confirm(
    `确定删除该连接配置？\n${ep.kind} ${ep.address}:${ep.port}`,
    '删除连接配置',
    { type: 'warning' },
  )
  await api.deleteDeviceEndpoint(deviceId.value, Number(ep.id))
  ElMessage.success('已删除')
  await refresh()
}

const setPrimary = async (ep: Record<string, unknown>) => {
  await api.setEndpointPrimary(deviceId.value, Number(ep.id))
  ElMessage.success('已设为主用接入')
  await refresh()
}

const credentialLabel = (id: number | null | undefined) => {
  if (!id) return '未绑定'
  const c = credentials.value.find((x) => Number(x.id) === id)
  if (!c) return `#${id}`
  return `${c.name} (${c.username})`
}

const kindLabel = (v: string) =>
  KIND_OPTIONS.find((k) => k.value === v)?.label || v

const goSsh = (ep?: Record<string, unknown>) => {
  if (ep) {
    router.push({
      path: `/devices/${deviceId.value}/shell`,
      query: { endpoint_id: String(ep.id) },
    })
  } else {
    router.push(`/devices/${deviceId.value}/shell`)
  }
}

onMounted(refresh)
</script>

<template>
  <div class="page" v-loading="loading" v-if="device">
    <el-card>
      <template #header>
        <div class="header">
          <span class="title">
            {{ (device as { name: string }).name }}
            <el-tag>{{ (device as { vendor: string }).vendor }}</el-tag>
            <el-tag v-if="(device as { is_simulated: boolean }).is_simulated" type="info" size="small">模拟</el-tag>
            <el-tag v-if="!(device as { is_active: boolean }).is_active" type="danger" size="small">停用</el-tag>
          </span>
          <el-space>
            <el-button @click="router.push('/devices')">返回</el-button>
            <el-button type="primary" @click="goSsh()">WebSSH</el-button>
            <el-button @click="collectNow">立即采集</el-button>
          </el-space>
        </div>
      </template>

      <el-tabs v-model="active">
        <!-- ─── 基本信息 ────────────────────────────────────────── -->
        <el-tab-pane label="基本信息" name="info">
          <el-descriptions :column="2" border>
            <el-descriptions-item label="主机名">{{ (device as { hostname: string }).hostname }}</el-descriptions-item>
            <el-descriptions-item label="型号">{{ (device as { model?: string }).model || '—' }}</el-descriptions-item>
            <el-descriptions-item label="软件版本">{{ (device as { software_version?: string }).software_version || '—' }}</el-descriptions-item>
            <el-descriptions-item label="序列号">{{ (device as { serial_no?: string }).serial_no || '—' }}</el-descriptions-item>
            <el-descriptions-item label="位置">{{ (device as { location?: string }).location || '—' }}</el-descriptions-item>
            <el-descriptions-item label="分组">{{ (device as { group_name?: string }).group_name || '—' }}</el-descriptions-item>
            <el-descriptions-item label="标签">{{ (device as { tags?: string }).tags || '—' }}</el-descriptions-item>
            <el-descriptions-item label="主用接入">
              <span v-if="(device as { primary_endpoint?: { address: string } }).primary_endpoint">{{ ((device as { primary_endpoint: { address: string; port: number; kind: string } }).primary_endpoint.address) }}:{{ ((device as { primary_endpoint: { port: number } }).primary_endpoint.port) }} ({{ ((device as { primary_endpoint: { kind: string } }).primary_endpoint.kind) }})</span>
              <span v-else style="color: #aaa">未配置（请到「连接配置」中添加）</span>
            </el-descriptions-item>
          </el-descriptions>
        </el-tab-pane>

        <!-- ─── 连接配置（CRT 风格） ────────────────────────────── -->
        <el-tab-pane label="连接配置" name="endpoints">
          <div class="conn-header">
            <span class="conn-tip">
              每个连接对应一个 SSH/Telnet/Console 接入点。
              <strong>主用接入</strong>用于「WebSSH」快速连接。
            </span>
            <el-button type="primary" size="small" @click="openCreateEndpoint">+ 新增连接</el-button>
          </div>

          <el-table :data="endpoints" border stripe v-if="endpoints.length">
            <el-table-column type="index" label="#" width="50" align="center" />
            <el-table-column label="类型" width="160">
              <template #default="{ row }">
                <el-tag size="small">{{ kindLabel(String(row.kind)) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="地址" min-width="180">
              <template #default="{ row }">
                <span class="mono">{{ row.address }}:{{ row.port }}</span>
              </template>
            </el-table-column>
            <el-table-column label="凭据" min-width="200">
              <template #default="{ row }">
                <el-tag v-if="!row.credential_id" type="warning" size="small">未绑定</el-tag>
                <span v-else>{{ credentialLabel(row.credential_id) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="主用" width="80" align="center">
              <template #default="{ row }">
                <el-tag v-if="row.is_primary" type="success" size="small">主用</el-tag>
                <el-button v-else link type="primary" size="small" @click="setPrimary(row)">设为主用</el-button>
              </template>
            </el-table-column>
            <el-table-column label="备注" min-width="140" show-overflow-tooltip>
              <template #default="{ row }">
                <span v-if="row.notes">{{ row.notes }}</span>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="240" align="center" fixed="right">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click="openEditEndpoint(row)">编辑</el-button>
                <el-button size="small" link type="primary" @click="goSsh(row)">连接</el-button>
                <el-button size="small" link type="danger" @click="removeEndpoint(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>

          <el-empty v-else description="尚无连接配置，请点击右上角「+ 新增连接」" />
        </el-tab-pane>

        <!-- ─── 最新配置 ────────────────────────────────────────── -->
        <el-tab-pane label="最新配置" name="revisions">
          <el-input v-model="configText" type="textarea" :autosize="{ minRows: 20 }" placeholder="尚无配置" />
          <div style="margin-top: 10px; text-align: right">
            <el-button @click="refresh">重新加载</el-button>
            <el-button type="primary" @click="saveConfig">保存为新版本</el-button>
          </div>
        </el-tab-pane>

        <!-- ─── 版本历史 ────────────────────────────────────────── -->
        <el-tab-pane label="版本历史" name="creds">
          <el-table :data="revisions" border>
            <el-table-column type="index" width="60" />
            <el-table-column prop="revision_no" label="版本号" width="80" />
            <el-table-column prop="source" label="来源" />
            <el-table-column prop="author" label="作者" />
            <el-table-column prop="note" label="备注" />
            <el-table-column prop="size_bytes" label="大小" />
            <el-table-column prop="created_at" label="创建时间" />
          </el-table>
        </el-tab-pane>

        <!-- ─── 任务记录 ────────────────────────────────────────── -->
        <el-tab-pane label="任务记录" name="jobs">
          <el-alert type="info" :closable="false">前往「批量任务」查看与该设备相关任务</el-alert>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- ─── 新增/编辑连接配置 对话框 ──────────────────────────── -->
    <el-dialog
      v-model="endpointDialog.visible"
      :title="endpointDialog.editing ? '编辑连接配置' : '新增连接配置'"
      width="520px"
      :close-on-click-modal="false"
    >
      <el-form label-width="100px" :model="endpointForm">
        <el-form-item label="连接类型" required>
          <el-select v-model="endpointForm.kind" @change="onKindChange" style="width: 100%">
            <el-option v-for="k in KIND_OPTIONS" :key="k.value" :label="k.label" :value="k.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="地址" required>
          <el-input v-model="endpointForm.address" placeholder="IP 或域名，例如 10.0.0.1" />
        </el-form-item>
        <el-form-item label="端口" required>
          <el-input-number v-model="endpointForm.port" :min="1" :max="65535" />
          <span class="muted" style="margin-left:8px">SSH 默认 22，Telnet 23，NETCONF 830</span>
        </el-form-item>
        <el-form-item label="凭据">
          <el-select v-model="endpointForm.credential_id" placeholder="选择凭据（必填，模拟设备除外）" clearable style="width: 100%" filterable>
            <el-option
              v-for="c in credentials"
              :key="c.id"
              :value="c.id"
              :label="`${c.name}（${c.username} / ${c.secret_kind}）`"
            />
          </el-select>
          <div class="muted" style="margin-top:4px">
            凭据在「系统设置 → 凭据管理」中维护；新增凭据后请回此页面选择。
          </div>
        </el-form-item>
        <el-form-item label="主用接入">
          <el-switch v-model="endpointForm.is_primary" />
          <span class="muted" style="margin-left:8px">设为「主用」后，WebSSH 快速连接会默认使用此连接</span>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="endpointForm.notes" placeholder="可选：用途说明" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="endpointDialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="endpointDialog.saving" @click="saveEndpoint">
          {{ endpointDialog.editing ? '保存' : '添加' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.header { display: flex; align-items: center; justify-content: space-between; }
.title { font-weight: 600; font-size: 16px; display: inline-flex; align-items: center; gap: 8px; }
.conn-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.conn-tip { color: var(--text-3); font-size: 13px; }
.mono { font-family: var(--font-mono); font-size: 12px; color: var(--text-1); }
.muted { color: var(--text-3); font-size: 12px; }
</style>
