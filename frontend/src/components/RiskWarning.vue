<!-- Enhanced Risk Warning Component with Animations -->
<template>
  <transition name="risk-slide">
    <div v-if="requiresApproval" class="risk-alert-wrapper">
      <div class="risk-alert">
        <div class="risk-icon-pulse">
          <el-icon class="risk-icon"><WarningFilled /></el-icon>
        </div>

        <div class="risk-content">
          <div class="risk-header">
            <span class="risk-badge">高危操作</span>
            <span class="risk-title">检测到危险命令 - 需要管理员审批</span>
          </div>

          <div class="risk-commands">
            <div class="command-label">
              <el-icon><DocumentCopy /></el-icon>
              <span>识别出 {{ riskCommands.length }} 条高危命令:</span>
            </div>
            <ul class="command-list">
              <li v-for="(cmd, idx) in riskCommands.slice(0, 5)" :key="idx" class="command-item">
                <el-icon class="cmd-icon"><CloseBold /></el-icon>
                <code class="cmd-text">{{ cmd }}</code>
              </li>
              <li v-if="riskCommands.length > 5" class="command-more">
                <el-icon><MoreFilled /></el-icon>
                <span>还有 {{ riskCommands.length - 5 }} 条高危命令...</span>
              </li>
            </ul>
          </div>

          <div class="risk-footer">
            <div class="approval-notice">
              <el-icon class="lock-icon"><Lock /></el-icon>
              <span>此任务必须经管理员审批后才能执行，以防止网络中断或配置丢失</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </transition>
</template>

<script setup lang="ts">
import { WarningFilled, Lock, DocumentCopy, CloseBold, MoreFilled } from '@element-plus/icons-vue'

defineProps<{
  requiresApproval: boolean
  riskCommands: string[]
}>()
</script>

<style scoped>
/* Slide animation */
.risk-slide-enter-active,
.risk-slide-leave-active {
  transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
}

.risk-slide-enter-from {
  opacity: 0;
  transform: translateY(-20px);
}

.risk-slide-leave-to {
  opacity: 0;
  transform: translateY(-10px);
}

.risk-alert-wrapper {
  margin: 20px 0;
}

.risk-alert {
  background: linear-gradient(135deg, #fef2f2 0%, #fee2e2 100%);
  border: 2px solid #fca5a5;
  border-left: 6px solid #dc2626;
  border-radius: 12px;
  padding: 24px;
  display: flex;
  gap: 20px;
  box-shadow: 0 8px 24px rgba(220, 38, 38, 0.15);
  position: relative;
  overflow: hidden;
}

.risk-alert::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 4px;
  background: linear-gradient(90deg, #dc2626, #ef4444, #f87171);
  animation: shimmer 2s ease-in-out infinite;
}

@keyframes shimmer {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}

.risk-icon-pulse {
  flex-shrink: 0;
  animation: pulse-scale 2s ease-in-out infinite;
}

@keyframes pulse-scale {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.1); }
}

.risk-icon {
  font-size: 40px;
  color: #dc2626;
  filter: drop-shadow(0 4px 8px rgba(220, 38, 38, 0.3));
}

.risk-content {
  flex: 1;
  min-width: 0;
}

.risk-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.risk-badge {
  display: inline-flex;
  align-items: center;
  padding: 4px 10px;
  background: #dc2626;
  color: #fff;
  font-size: 11px;
  font-weight: 700;
  border-radius: 6px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  box-shadow: 0 2px 8px rgba(220, 38, 38, 0.3);
}

.risk-title {
  font-size: 16px;
  font-weight: 700;
  color: #991b1b;
}

.risk-commands {
  margin: 16px 0;
}

.command-label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  color: #7f1d1d;
  margin-bottom: 12px;
}

.command-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.command-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  background: rgba(255, 255, 255, 0.8);
  border: 1px solid #fca5a5;
  border-radius: 8px;
  transition: all 0.2s ease;
}

.command-item:hover {
  background: #fff;
  box-shadow: 0 2px 8px rgba(220, 38, 38, 0.1);
  transform: translateX(4px);
}

.cmd-icon {
  font-size: 14px;
  color: #dc2626;
  flex-shrink: 0;
}

.cmd-text {
  flex: 1;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 13px;
  color: #7f1d1d;
  background: rgba(220, 38, 38, 0.05);
  padding: 4px 8px;
  border-radius: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.command-more {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  font-size: 12px;
  color: #b91c1c;
  font-style: italic;
}

.risk-footer {
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px dashed #fca5a5;
}

.approval-notice {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  background: rgba(220, 38, 38, 0.08);
  border-radius: 8px;
  font-size: 13px;
  color: #7f1d1d;
  line-height: 1.5;
}

.lock-icon {
  font-size: 18px;
  color: #dc2626;
  flex-shrink: 0;
}
</style>
