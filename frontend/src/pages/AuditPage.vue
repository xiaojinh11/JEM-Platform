<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api'

const events = ref<Record<string, unknown>[]>([])
const loading = ref(false)
const filters = reactive({ actor: '', action: '', since_days: 7 })

async function exportData() {
  try {
    await api.exportData('audit', 'json')
    ElMessage.success('审计日志已导出')
  } catch (err: unknown) {
    const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail ?? '导出失败')
  }
}

const refresh = async () => {
  loading.value = true
  try {
    events.value = await api.listAudit({
      actor: filters.actor || undefined,
      action: filters.action || undefined,
      since_days: filters.since_days,
    })
  } finally {
    loading.value = false
  }
}

const actionTag = (a: string) => {
  if (a.startsWith('login')) return 'primary'
  if (a.includes('create') || a.includes('success')) return 'success'
  if (a.includes('delete') || a.includes('fail')) return 'danger'
  if (a.includes('update')) return 'warning'
  return 'info'
}

onMounted(refresh)
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="page-header">
      <span class="page-title">审计日志</span>
      <div class="header-right">
        <el-input v-model="filters.actor" placeholder="操作者" style="width:150px" @keyup.enter="refresh" clearable />
        <el-input v-model="filters.action" placeholder="动作前缀" style="width:180px" @keyup.enter="refresh" clearable />
        <el-input-number v-model="filters.since_days" :min="1" :max="365" @change="refresh" />
        <el-button type="primary" @click="refresh">查询</el-button>
        <el-button @click="exportData">导出</el-button>
      </div>
    </div>

    <div class="table-card">
      <el-table :data="events" border stripe size="small">
        <el-table-column type="index" label="#" width="50" align="center" />
        <el-table-column prop="created_at" label="时间" width="185" />
        <el-table-column prop="actor" label="操作者" width="110" />
        <el-table-column label="动作" width="160">
          <template #default="{ row }">
            <el-tag size="small" :type="actionTag(String(row.action))" effect="dark">{{ row.action }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="target_kind" label="对象类型" width="110" />
        <el-table-column prop="target_id" label="对象ID" width="80" align="center" />
        <el-table-column prop="ip_address" label="来源IP" width="150" />
        <el-table-column prop="detail" label="详情" min-width="200" show-overflow-tooltip />
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
.header-right {
  display: flex;
  gap: 10px;
  align-items: center;
}
.table-card {
  background: var(--bg-canvas);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  padding: 4px 18px 18px;
  color: var(--text-1);
}

/* Force table cells to use the strongest theme text color */
.table-card :deep(.el-table .el-table__cell) {
  background: transparent !important;
  color: var(--text-1) !important;
}
.table-card :deep(.el-table th.el-table__cell) {
  background: var(--bg-panel) !important;
  color: var(--text-1) !important;
  font-weight: 600;
}
.table-card :deep(.el-table .cell) {
  color: var(--text-1);
}

/* Force tag colors — element-plus defaults look washed-out in dark mode */
.table-card :deep(.el-tag) {
  border: none !important;
  color: #fff !important;
  font-weight: 600;
}
.table-card :deep(.el-tag--primary)  { background: var(--accent)        !important; }
.table-card :deep(.el-tag--success)  { background: var(--success)       !important; }
.table-card :deep(.el-tag--danger)   { background: var(--danger)        !important; }
.table-card :deep(.el-tag--warning)  { background: var(--warning)       !important; }
.table-card :deep(.el-tag--info)     { background: var(--border-3)      !important; }
</style>
