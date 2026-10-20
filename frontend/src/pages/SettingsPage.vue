<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import { useAuthStore } from '@/stores/auth'
import JellyRadioWrapper from '@/components/react/JellyRadioWrapper.vue'

const auth = useAuthStore()
const adapters = ref<Array<{ vendor: string; compatibility_level: number; capabilities: string[] }>>([])
const credentials = ref<Record<string, unknown>[]>([])
const users = ref<Record<string, unknown>[]>([])
const loading = ref(false)

const credForm = reactive({
  name: '', username: '', password: '', ssh_key: '',
  enable_password: '', secret_kind: 'password', description: '',
})
const userForm = reactive({
  username: '', password: '', role: 'operator', full_name: '',
})
const pwForm   = reactive({ old_password: '', new_password: '' })

// ── Export ─────────────────────────────────────────────────────────
const exportItems = ref<{ scope: string; label: string; desc: string; count: number }[]>([
  { scope: 'devices',    label: '设备台账',     desc: '设备名称、厂商、型号、IP、角色、标签等（不含密码）', count: 0 },
  { scope: 'credentials', label: '凭据管理',  desc: '凭据名称、用户名、类型（不含密码明文）', count: 0 },
  { scope: 'documents',   label: '手册库',     desc: '文档标题、厂商、分类、版本、状态等', count: 0 },
  { scope: 'configs',     label: '配置中心',   desc: '配置快照、版本号、时间等', count: 0 },
  { scope: 'drift',       label: '配置漂移',   desc: '漂移摘要、严重性、设备、时间等', count: 0 },
  { scope: 'audit',       label: '审计日志',   desc: '操作记录、用户、IP、时间等', count: 0 },
])

async function doExport(scope: string, format: 'json' | 'csv') {
  try {
    await api.exportData(scope, format)
  } catch (err: unknown) {
    const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail ?? '导出失败')
  }
}

// Load export counts on mount (fire-and-forget; one request per scope)
async function loadExportCounts() {
  for (const item of exportItems.value) {
    try {
      const data = await api.exportPreview(item.scope)
      item.count = data.records.length
    } catch {
      item.count = 0
    }
  }
}

const refresh = async () => {
  loading.value = true
  try {
    adapters.value = (await api.listAdapters()) as typeof adapters.value
    credentials.value = await api.listCredentials()
    try {
      users.value = await api.listUsers()
    } catch (e) {
      users.value = []
    }
  } finally {
    loading.value = false
  }
}

// ── Pending registrations (operator + inactive) ──
const pendingUsers = computed(() =>
  (users.value as Array<{
    id: number; username: string; email: string; full_name: string | null;
    role: string; is_active: boolean; created_at: string; last_login_at?: string | null
  }>).filter((u) => !u.is_active && u.role === 'operator')
)

async function approveUser(userId: number) {
  try {
    await ElMessageBox.confirm(
      `确定要通过该用户的注册申请吗？激活后该用户可正常登录。`,
      '审核注册申请',
      { confirmButtonText: '确认通过', cancelButtonText: '取消', type: 'info' },
    )
  } catch {
    return
  }
  try {
    await api.approveUser(userId)
    ElMessage.success('该账户已激活，用户可正常登录')
    await refresh()
  } catch (err: unknown) {
    const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '操作失败'
    ElMessage.error(detail)
  }
}

const createCred = async () => {
  if (!credForm.name || !credForm.username) {
    ElMessage.warning('请填写凭据名称与用户名')
    return
  }
  const payload: Record<string, unknown> = {
    name: credForm.name, username: credForm.username,
    secret_kind: credForm.secret_kind, description: credForm.description,
  }
  if (credForm.secret_kind === 'ssh_key') payload.ssh_key = credForm.ssh_key
  else payload.password = credForm.password
  if (credForm.enable_password) payload.enable_password = credForm.enable_password
  await api.createCredential(payload)
  ElMessage.success('凭据已保存')
  await refresh()
  Object.assign(credForm, {
    name: '', username: '', password: '', ssh_key: '',
    enable_password: '', description: '',
  })
}

const deleteCred = async (id: number) => {
  await ElMessageBox.confirm('确认删除该凭据？', '提示', { type: 'warning' })
  await api.deleteCredential(id)
  ElMessage.success('已删除')
  await refresh()
}

const createUser = async () => {
  if (!userForm.username || !userForm.password) {
    ElMessage.warning('请填写用户名与密码')
    return
  }
  await api.createUser({ ...userForm })
  ElMessage.success('用户已创建')
  await refresh()
  Object.assign(userForm, { username: '', password: '', role: 'operator', full_name: '' })
}

const changePw = async () => {
  if (!pwForm.old_password || !pwForm.new_password) {
    ElMessage.warning('请填写完整')
    return
  }
  if (pwForm.new_password.length < 8) {
    ElMessage.warning('新密码至少 8 位')
    return
  }
  await api.changePassword(pwForm)
  ElMessage.success('密码已修改')
  Object.assign(pwForm, { old_password: '', new_password: '' })
}

const levelTone = (lvl: number) => lvl >= 3 ? 'success' : lvl >= 2 ? 'primary' : 'warning'
const levelText = (lvl: number) => `L${lvl}`

const isAdmin = computed(() => {
  const role = (auth.user as { role?: string } | null)?.role
  return role === 'admin'
})

const roles = [
  { value: 'admin',    label: '管理员', tone: 'danger'  },
  { value: 'operator', label: '操作员', tone: 'primary' },
  { value: 'viewer',   label: '只读',   tone: 'info'    },
]

onMounted(() => {
  refresh()
  loadExportCounts()
})
</script>

<template>
  <div class="page" v-loading="loading">
    <el-card>
      <template #header>
        <div class="card-header-row">
          <div>
            <div class="card-title">系统设置</div>
            <div class="card-sub">适配器 · 凭据 · 用户 · 密码</div>
          </div>
        </div>
      </template>

      <el-tabs class="page-tabs">
        <!-- 适配器 -->
        <el-tab-pane label="适配器矩阵">
          <el-table :data="adapters" border stripe empty-text="暂无适配器">
            <el-table-column label="厂商" min-width="180">
              <template #default="{ row }">
                <div class="vendor-cell">
                  <div class="vendor-badge">{{ String(row.vendor).slice(0, 2).toUpperCase() }}</div>
                  <span class="mono">{{ row.vendor }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="兼容性" min-width="120">
              <template #default="{ row }">
                <el-tag :type="levelTone(row.compatibility_level)" effect="dark">
                  {{ levelText(row.compatibility_level) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="能力" min-width="320">
              <template #default="{ row }">
                <el-tag
                  v-for="c in row.capabilities" :key="c" size="small"
                  effect="plain" class="cap-tag"
                >{{ c }}</el-tag>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- 凭据 -->
        <el-tab-pane label="凭据管理">
          <el-card class="sub-card" shadow="never">
            <template #header><span class="sub-title">新增凭据</span></template>
            <el-form label-width="100px" class="cred-form">
              <el-row :gutter="16">
                <el-col :xs="24" :sm="12">
                  <el-form-item label="名称" required>
                    <el-input v-model="credForm.name" placeholder="便于识别的凭据名称" />
                  </el-form-item>
                </el-col>
                <el-col :xs="24" :sm="12">
                  <el-form-item label="用户名" required>
                    <el-input v-model="credForm.username" placeholder="登录用户名" />
                  </el-form-item>
                </el-col>
                <el-col :xs="24" :sm="12">
                  <el-form-item label="类型">
                    <JellyRadioWrapper
                      v-model="credForm.secret_kind"
                      :items="[
                        { value: 'password', label: '密码' },
                        { value: 'ssh_key', label: 'SSH 私钥' },
                      ]"
                      aria-label="凭据类型"
                    />
                  </el-form-item>
                </el-col>
                <el-col :xs="24" :sm="12">
                  <el-form-item label="备注">
                    <el-input v-model="credForm.description" placeholder="用途 · 所属客户等" />
                  </el-form-item>
                </el-col>
              </el-row>

              <el-form-item v-if="credForm.secret_kind === 'password'" label="密码">
                <el-input v-model="credForm.password" type="password" show-password placeholder="登录密码" />
              </el-form-item>
              <el-form-item v-else label="私钥">
                <el-input
                  v-model="credForm.ssh_key" type="textarea"
                  :autosize="{ minRows: 4, maxRows: 10 }"
                  placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                  style="font-family: 'JetBrains Mono', monospace; font-size: 12.5px"
                />
              </el-form-item>
              <el-form-item label="enable">
                <el-input v-model="credForm.enable_password" type="password" show-password placeholder="特权密码（可选）" />
              </el-form-item>
              <el-form-item>
                <el-button type="primary" @click="createCred">
                  <el-icon><Plus /></el-icon> 保存凭据
                </el-button>
              </el-form-item>
            </el-form>
          </el-card>

          <el-card class="sub-card" shadow="never">
            <template #header>
              <div class="card-header-row">
                <span class="sub-title">已保存凭据（{{ credentials.length }}）</span>
              </div>
            </template>
            <el-table :data="credentials" border stripe empty-text="暂无凭据">
              <el-table-column prop="id" label="ID" width="64" align="center" />
              <el-table-column prop="name" label="名称" min-width="160" show-overflow-tooltip />
              <el-table-column prop="username" label="用户名" min-width="140" show-overflow-tooltip />
              <el-table-column label="类型" width="100">
                <template #default="{ row }">
                  <el-tag :type="(row as { secret_kind: string }).secret_kind === 'ssh_key' ? 'warning' : 'primary'" size="small" effect="light">
                    {{ (row as { secret_kind: string }).secret_kind === 'ssh_key' ? 'SSH Key' : 'Password' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="description" label="备注" min-width="200" show-overflow-tooltip>
                <template #default="{ row }">
                  <span v-if="(row as { description: string }).description">{{ (row as { description: string }).description }}</span>
                  <span v-else class="text-muted">—</span>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="100" align="center" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" link type="danger" @click="deleteCred((row as { id: number }).id)">删除</el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-card>
        </el-tab-pane>

        <!-- 用户 -->
        <el-tab-pane label="用户管理">
          <el-card class="sub-card" shadow="never">
            <template #header><span class="sub-title">新增用户</span></template>
            <el-form label-width="100px" class="cred-form">
              <el-row :gutter="16">
                <el-col :xs="24" :sm="12" :md="6">
                  <el-form-item label="用户名" required>
                    <el-input v-model="userForm.username" placeholder="登录用户名" />
                  </el-form-item>
                </el-col>
                <el-col :xs="24" :sm="12" :md="6">
                  <el-form-item label="密码" required>
                    <el-input v-model="userForm.password" type="password" show-password placeholder="至少 8 位" />
                  </el-form-item>
                </el-col>
                <el-col :xs="24" :sm="12" :md="6">
                  <el-form-item label="角色">
                    <el-select v-model="userForm.role" :teleported="false" style="width: 100%">
                      <el-option
                        v-for="r in roles" :key="r.value"
                        :value="r.value" :label="r.label"
                      />
                    </el-select>
                  </el-form-item>
                </el-col>
                <el-col :xs="24" :sm="12" :md="6">
                  <el-form-item label="姓名">
                    <el-input v-model="userForm.full_name" placeholder="全名（可选）" />
                  </el-form-item>
                </el-col>
              </el-row>
              <el-form-item>
                <el-button type="primary" @click="createUser">
                  <el-icon><Plus /></el-icon> 创建用户
                </el-button>
              </el-form-item>
            </el-form>
          </el-card>

          <el-card class="sub-card" shadow="never">
            <template #header>
              <div class="card-header-row">
                <span class="sub-title">用户列表（{{ users.length }}）</span>
              </div>
            </template>
            <el-table :data="users" border stripe empty-text="暂无用户">
              <el-table-column prop="id" label="ID" width="64" align="center" />
              <el-table-column label="用户名" min-width="160">
                <template #default="{ row }">
                  <div class="actor-cell">
                    <div class="actor-avatar">{{ String((row as { username: string }).username).slice(0, 1).toUpperCase() }}</div>
                    <span>{{ (row as { username: string }).username }}</span>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="角色" width="120">
                <template #default="{ row }">
                  <el-tag :type="roleTone(String((row as { role: string }).role))" size="small" effect="light">
                    {{ roleLabel(String((row as { role: string }).role)) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="full_name" label="姓名" min-width="140" show-overflow-tooltip>
                <template #default="{ row }">
                  <span v-if="(row as { full_name: string }).full_name">{{ (row as { full_name: string }).full_name }}</span>
                  <span v-else class="text-muted">—</span>
                </template>
              </el-table-column>
              <el-table-column prop="last_login_at" label="最后登录" min-width="200" show-overflow-tooltip>
                <template #default="{ row }">
                  <span class="mono">{{ (row as { last_login_at?: string }).last_login_at || '从未' }}</span>
                </template>
              </el-table-column>
            </el-table>
          </el-card>
        </el-tab-pane>

        <!-- 注册审核（仅管理员可见） -->
        <el-tab-pane v-if="isAdmin" label="注册审核">
          <el-alert
            v-if="pendingUsers.length === 0"
            type="success"
            :closable="false"
            show-icon
            style="margin-bottom: 16px"
          >
            <template #title>当前没有待审核的注册申请</template>
          </el-alert>
          <el-alert
            v-else
            type="warning"
            :closable="false"
            show-icon
            style="margin-bottom: 16px"
          >
            <template #title>
              共有 {{ pendingUsers.length }} 个待审核注册申请，点击「通过审核」激活账户
            </template>
          </el-alert>

          <el-card class="sub-card" shadow="never">
            <template #header>
              <div class="card-header-row">
                <span class="sub-title">待审核用户（{{ pendingUsers.length }}）</span>
              </div>
            </template>
            <el-table :data="pendingUsers" border stripe empty-text="暂无待审核用户">
              <el-table-column label="状态" width="90" align="center">
                <template #default>
                  <el-tag type="warning" size="small" effect="dark">待审核</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="用户名" min-width="140">
                <template #default="{ row }">
                  <span class="mono">{{ row.username }}</span>
                </template>
              </el-table-column>
              <el-table-column label="邮箱" min-width="200">
                <template #default="{ row }">
                  <span class="mono">{{ row.email }}</span>
                </template>
              </el-table-column>
              <el-table-column label="姓名" min-width="120">
                <template #default="{ row }">
                  <span class="mono">{{ row.full_name || '—' }}</span>
                </template>
              </el-table-column>
              <el-table-column label="注册时间" min-width="180">
                <template #default="{ row }">
                  <span class="mono dim">{{ row.created_at?.replace('T', ' ').slice(0, 19) || '—' }}</span>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="130" align="center" fixed="right">
                <template #default="{ row }">
                  <el-button type="primary" size="small" @click="approveUser(row.id)">
                    <el-icon><Check /></el-icon>
                    通过审核
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-card>
        </el-tab-pane>

        <!-- 修改密码 -->
        <el-tab-pane label="数据导出">
          <el-card class="sub-card" shadow="never" style="max-width: 800px">
            <p style="color:#909399;font-size:13px;margin:0 0 16px">
              将平台上所有数据导出为本地文件，支持 JSON（单文件）和 CSV/ZIP（全量分表）两种格式。
              敏感内容（密码、SSH 密钥）不会出现在导出文件中。
            </p>
            <el-table :data="exportItems" border stripe style="margin-bottom:16px">
              <el-table-column prop="label" label="数据模块" min-width="160" />
              <el-table-column prop="desc" label="内容说明" min-width="300" show-overflow-tooltip />
              <el-table-column prop="count" label="当前条数" width="100" align="center" />
              <el-table-column label="操作" width="200" align="center" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" @click="doExport(row.scope, 'json')">JSON</el-button>
                  <el-button size="small" @click="doExport(row.scope, 'csv')">CSV/ZIP</el-button>
                </template>
              </el-table-column>
            </el-table>
            <el-divider />
            <el-space wrap style="margin-top:12px">
              <el-button type="primary" @click="doExport('all', 'json')">导出全部（JSON）</el-button>
              <el-button type="primary" @click="doExport('all', 'csv')">导出全部（CSV/ZIP）</el-button>
            </el-space>
          </el-card>
        </el-tab-pane>

        <el-tab-pane label="修改我的密码">
          <el-card class="sub-card" shadow="never" style="max-width: 520px">
            <el-alert
              type="warning"
              :closable="false"
              show-icon
              style="margin-bottom: 18px"
            >
              <template #title>修改成功后会保留登录状态，但下次登录请使用新密码</template>
            </el-alert>
            <el-form label-width="110px">
              <el-form-item label="旧密码">
                <el-input v-model="pwForm.old_password" type="password" show-password />
              </el-form-item>
              <el-form-item label="新密码">
                <el-input v-model="pwForm.new_password" type="password" show-password />
              </el-form-item>
              <el-form-item label="确认新密码">
                <el-input v-model="pwForm.new_password" type="password" show-password />
              </el-form-item>
              <el-form-item>
                <el-button type="primary" @click="changePw">
                  <el-icon><Check /></el-icon> 修改密码
                </el-button>
              </el-form-item>
            </el-form>
          </el-card>
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<script lang="ts">
function roleTone(r: string) {
  if (r === 'admin') return 'danger'
  if (r === 'operator') return 'primary'
  return 'info'
}
function roleLabel(r: string) {
  if (r === 'admin') return '管理员'
  if (r === 'operator') return '操作员'
  return '只读'
}
</script>

<style scoped>
.card-header-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.card-title { font-size: 16px; font-weight: 700; color: var(--text-1); }
.card-sub { font-size: 12px; color: var(--text-3); margin-top: 4px; }
.sub-title { font-weight: 600; font-size: 14px; color: var(--text-1); }

.page-tabs :deep(.el-tabs__content) { padding-top: 16px; }
.sub-card { margin-bottom: 16px !important; }

/* ===== 厂商单元格 ===== */
.vendor-cell { display: inline-flex; align-items: center; gap: 10px; }
.vendor-badge {
  width: 32px; height: 32px;
  border-radius: 8px;
  background: var(--brand-gradient);
  color: #fff;
  display: flex; align-items: center; justify-content: center;
  font-weight: 700; font-size: 11px;
}
.mono { font-family: 'JetBrains Mono', monospace; font-size: 13px; color: var(--text-1); }
.cap-tag { margin-right: 6px; margin-bottom: 4px; }

.actor-cell { display: inline-flex; align-items: center; gap: 8px; }
.actor-avatar {
  width: 24px; height: 24px;
  border-radius: 50%;
  background: var(--brand-gradient);
  color: #fff;
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 600;
}

.text-muted { color: var(--text-4); font-size: 12px; }
.cred-form .el-form-item { margin-bottom: 18px; }
</style>
