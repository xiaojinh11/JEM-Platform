<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'

const router = useRouter()
const list = ref<Record<string, unknown>[]>([])
const loading = ref(false)

const filters = reactive({
  q: '',
  vendor: '',
  role: '',
  group: '',
})

const vendorMap: Record<string, string> = {
  huawei_vrp: '华为 VRP',
  cisco_ios_xe: '思科 IOS-XE',
  ruijie_rgos: '锐捷 RGOS',
  simulated: '模拟设备',
}

const vendorTagType = (v: string): '' | 'success' | 'warning' | 'info' | 'danger' | 'primary' => {
  if (v === 'huawei_vrp') return 'danger'
  if (v === 'cisco_ios_xe') return 'primary'
  if (v === 'ruijie_rgos') return 'success'
  return 'info'
}

const refresh = async () => {
  loading.value = true
  try {
    const devs = await api.listDevices({
      q: filters.q || undefined,
      vendor: filters.vendor || undefined,
      role: filters.role || undefined,
    })
    // Backend returns a flat array; guard against paginated shape.
    list.value = Array.isArray((devs as { items?: Record<string, unknown>[] }).items)
      ? (devs as { items: Record<string, unknown>[] }).items
      : (devs as Record<string, unknown>[])
  } finally {
    loading.value = false
  }
}

const reset = () => {
  filters.q = ''; filters.vendor = ''; filters.role = ''; filters.group = ''
  refresh()
}

const onDelete = async (id: number) => {
  await ElMessageBox.confirm('确认删除该设备？此操作不可撤销', '删除设备', { type: 'warning' })
  await api.deleteDevice(id)
  ElMessage.success('已删除')
  refresh()
}

const importCsv = async () => {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.csv'
  input.onchange = async () => {
    if (!input.files?.[0]) return
    const res = await api.importCsv(input.files[0], 'huawei_vrp')
    ElMessage.success(`导入完成：共 ${res.total} 条，新增 ${res.inserted} 条，更新 ${res.updated} 条`)
    refresh()
  }
  input.click()
}

async function exportData() {
  try {
    await api.exportData('devices', 'json')
    ElMessage.success('设备数据已导出')
  } catch (err: unknown) {
    const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail ?? '导出失败')
  }
}

onMounted(refresh)
</script>

<template>
  <div class="page" v-loading="loading">

    <!-- 标题 -->
    <div class="page-header">
      <span class="page-title">设备台账</span>
    </div>

    <!-- 筛选栏 -->
    <div class="filter-bar">
      <el-input
        v-model="filters.q"
        placeholder="搜索名称 / IP / SN"
        clearable
        @keyup.enter="refresh"
        class="filter-input"
        @clear="refresh"
      >
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>

      <el-select v-model="filters.vendor" clearable placeholder="全部厂商" @change="refresh" class="filter-select">
        <el-option v-for="(label, value) in vendorMap" :key="value" :value="value" :label="label" />
      </el-select>

      <el-input
        v-model="filters.group"
        placeholder="分组"
        clearable
        @keyup.enter="refresh"
        class="filter-input-sm"
        @clear="refresh"
      />

      <el-button @click="refresh">查询</el-button>
      <el-button @click="reset">重置</el-button>
      <el-button type="primary" @click="importCsv">导入 CSV</el-button>
      <el-button @click="exportData">导出</el-button>
    </div>

    <!-- 数据表格 -->
    <div class="table-card">
      <div class="table-top">
        <span class="table-count">共 {{ list.length }} 条</span>
        <el-button @click="refresh" size="small">刷新</el-button>
        <el-button size="small" @click="exportData">导出 JSON</el-button>
      </div>

      <el-table :data="list" border stripe size="small">
        <el-table-column type="index" label="#" width="50" align="center" />
        <el-table-column prop="name" label="名称" min-width="140" show-overflow-tooltip />
        <el-table-column prop="hostname" label="主机名" min-width="140" show-overflow-tooltip />
        <el-table-column label="厂商" width="130">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="vendorTagType(String(row.vendor))"
              effect="plain"
            >{{ vendorMap[String(row.vendor)] || row.vendor }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="role" label="角色" width="90" />
        <el-table-column prop="group_name" label="分组" min-width="110" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.group_name">{{ row.group_name }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="location" label="位置" min-width="110" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.location">{{ row.location }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="主用接入" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <template v-if="row.primary_endpoint">
              <span class="mono">{{ row.primary_endpoint.address }}:{{ row.primary_endpoint.port }}</span>
              <el-tag size="small" type="info" effect="plain" style="margin-left:6px">{{ row.primary_endpoint.kind }}</el-tag>
            </template>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120" align="center">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'danger'" size="small" effect="dark">
              {{ row.is_active ? '在用' : '停用' }}
            </el-tag>
            <el-tag v-if="row.is_simulated" size="small" type="info" style="margin-left:4px">模拟</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="240" align="center" fixed="right">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="router.push(`/devices/${row.id}`)">详情</el-button>
            <el-button
              size="small"
              link
              type="success"
              :disabled="!row.primary_endpoint"
              @click="router.push(`/devices/${row.id}/shell`)"
            >Console</el-button>
            <el-button size="small" link type="primary" @click="router.push('/shell')">多终端</el-button>
            <el-button size="small" link type="danger" @click="onDelete(row.id)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* ===== 筛选栏 ===== */
.filter-bar {
  background: var(--bg-canvas);
  border-radius: var(--radius-md);
  border: 1px solid var(--border-1);
  padding: 12px 16px;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  color: var(--text-1);
}
.filter-input { width: 200px; }
.filter-input-sm { width: 140px; }
.filter-select { width: 160px; }

/* ===== 表格卡片 ===== */
.table-card {
  background: var(--bg-canvas);
  border-radius: var(--radius-md);
  border: 1px solid var(--border-1);
  padding: 4px 18px 18px;
  color: var(--text-1);
}

.table-top {
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-1);
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.table-count {
  font-size: 13px;
  color: var(--text-3);
}

.muted { color: var(--text-3); font-size: 12px; }
.mono { font-family: var(--font-mono); font-size: 12px; color: var(--text-1); }

/* Page title with cyan accent bar */
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
</style>
