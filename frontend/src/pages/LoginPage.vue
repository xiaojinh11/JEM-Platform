<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import axios from 'axios'
import { useAuthStore } from '@/stores/auth'
import { useThemeStore } from '@/stores/theme'
import { ElMessage } from 'element-plus'
import ParticleBackground from '@/components/ParticleBackground.vue'

// Public axios instance for the auth pages — we deliberately bypass the
// `api` instance here because those calls happen before login and we
// don't want the JWT interceptor to look at the (empty) token on every
// registration code request.
const http = axios.create({ baseURL: '/api/v1', timeout: 30000 })

const router = useRouter()
const auth = useAuthStore()
const theme = useThemeStore()

type Mode = 'login' | 'register' | 'forgot' | 'reset'
const mode = ref<Mode>('login')

const loginForm = reactive({ username: '', password: '' })
const registerForm = reactive({ email: '', code: '', username: '', password: '', confirm: '', full_name: '' })
const forgotForm = reactive({ email: '' })
const resetForm = reactive({ token: '', newPassword: '', confirm: '' })

const loading = ref(false)
const countdown = ref(0)  // 验证码倒计时
const resetTokenValid = ref<boolean | null>(null)
const isLight = computed(() => theme.theme === 'light')

// 从 URL hash 中读取 reset token，自动跳转到重置密码页
if (typeof window !== 'undefined') {
  const hash = window.location.hash
  const match = hash.match(/[#&?]token=([^&]+)/)
  if (match) {
    resetForm.token = decodeURIComponent(match[1])
    mode.value = 'reset'
  }
}

async function sendRegisterCode() {
  if (!registerForm.email) {
    ElMessage.warning('请输入邮箱')
    return
  }
  loading.value = true
  try {
    const r = await http.post('/auth/register/request-code', { email: registerForm.email })
    if (r.data?.ok) {
      // smtp_sent=false means no SMTP configured → code was logged to backend console
      if (!r.data.smtp_sent) {
        ElMessage({
          message: '验证码已发送，请查看后端控制台窗口（[DEV MAIL]）',
          type: 'warning',
          duration: 0,
          showClose: true,
        })
      } else {
        ElMessage.success(r.data.message || '验证码已发送，请查收邮件')
      }
      countdown.value = 60
      const timer = setInterval(() => {
        countdown.value -= 1
        if (countdown.value <= 0) clearInterval(timer)
      }, 1000)
    }
  } catch (err: unknown) {
    const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '发送失败'
    ElMessage.error(detail)
  } finally {
    loading.value = false
  }
}

async function submitRegister() {
  if (!registerForm.email || !registerForm.code || !registerForm.username || !registerForm.password) {
    ElMessage.warning('请填写完整信息')
    return
  }
  if (registerForm.password !== registerForm.confirm) {
    ElMessage.warning('两次输入的密码不一致')
    return
  }
  loading.value = true
  try {
    const r = await http.post('/auth/register/verify', {
      email: registerForm.email,
      code: registerForm.code,
      username: registerForm.username,
      password: registerForm.password,
      full_name: registerForm.full_name || undefined,
    })
    if (r.data?.ok) {
      ElMessage.success(r.data.message || '注册成功，请等待管理员审核')
      mode.value = 'login'
      // 清空注册表单
      registerForm.email = ''
      registerForm.code = ''
      registerForm.username = ''
      registerForm.password = ''
      registerForm.confirm = ''
      registerForm.full_name = ''
    }
  } catch (err: unknown) {
    const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '注册失败'
    ElMessage.error(detail)
  } finally {
    loading.value = false
  }
}

async function submitForgot() {
  if (!forgotForm.email) {
    ElMessage.warning('请输入注册时使用的邮箱')
    return
  }
  loading.value = true
  try {
    const r = await http.post('/auth/password-reset/request', { email: forgotForm.email })
    if (r.data?.ok) {
      ElMessage.success(r.data.message || '如果该邮箱已注册，重置链接已发送')
      mode.value = 'login'
      forgotForm.email = ''
    }
  } catch (err: unknown) {
    const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '发送失败'
    ElMessage.error(detail)
  } finally {
    loading.value = false
  }
}

async function checkResetToken() {
  if (!resetForm.token) {
    resetTokenValid.value = false
    return
  }
  try {
    const r = await http.get('/auth/password-reset/validate', { params: { token: resetForm.token } })
    resetTokenValid.value = r.data?.ok === true
  } catch {
    resetTokenValid.value = false
  }
}

async function submitReset() {
  if (!resetForm.newPassword || !resetForm.confirm) {
    ElMessage.warning('请填写新密码')
    return
  }
  if (resetForm.newPassword !== resetForm.confirm) {
    ElMessage.warning('两次输入的密码不一致')
    return
  }
  loading.value = true
  try {
    const r = await http.post('/auth/password-reset/confirm', {
      token: resetForm.token,
      new_password: resetForm.newPassword,
    })
    if (r.data?.ok) {
      ElMessage.success(r.data.message || '密码已重置，请使用新密码登录')
      // 清理 URL 中的 token
      window.history.replaceState(null, '', window.location.pathname + window.location.search + '#/login')
      mode.value = 'login'
      resetForm.token = ''
      resetForm.newPassword = ''
      resetForm.confirm = ''
    }
  } catch (err: unknown) {
    const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '重置失败'
    ElMessage.error(detail)
  } finally {
    loading.value = false
  }
}

const login = async () => {
  if (!loginForm.username || !loginForm.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  // Wipe any stale token + user state BEFORE we issue the new login
  // request. The previous behaviour was to call api.login first and
  // then overwrite localStorage — but if /auth/me in ``auth.login``
  // raced with stale-state reactive updates, the UI sometimes ended
  // up showing the expired-session popup right after a fresh login.
  // Clearing first guarantees ``auth.token`` only carries the new JWT.
  auth.clearSession('logout')
  try {
    await auth.login(loginForm)
    ElMessage.success('登录成功')
    router.push('/dashboard')
  } catch (err: unknown) {
    const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '登录失败'
    ElMessage.error(detail)
  } finally {
    loading.value = false
  }
}

function switchMode(next: Mode) {
  mode.value = next
  if (next === 'reset' && resetForm.token) checkResetToken()
}
</script>

<template>
  <div class="login-page">
    <!-- Particle background covers the entire page.
         Tuned for "flowing field + mouse halo" rather than dense grid. -->
    <ParticleBackground :density="80" :speed="0.55" :link-distance="140" :mouse-radius="260" :flow-strength="0.4" />

    <!-- Theme toggle (top-right) -->
    <button class="theme-toggle" @click="theme.toggle()" :aria-label="isLight ? '切换到黑夜' : '切换到白天'">
      <el-icon v-if="isLight" :size="18"><Moon /></el-icon>
      <el-icon v-else :size="18"><Sunny /></el-icon>
      <span>{{ isLight ? '夜间' : '日间' }}</span>
    </button>

    <!-- Left brand panel -->
    <div class="login-left">
      <div class="left-content">
        <div class="brand-mark">
          <div class="brand-logo">
            <img src="/logo.svg" alt="JEM Logo" class="brand-logo-svg" />
          </div>
          <div class="brand-text">
            <div class="brand-name-main">JEM</div>
            <div class="brand-name-sub">Automated Operations Platform</div>
          </div>
        </div>

        <div class="left-divider"></div>

        <p class="left-slogan">
          面向多厂商网络设备的<br />自动化运营平台
        </p>

        <div class="feature-list">
          <div class="feature-item">
            <span class="feature-dot"></span>
            <span>多厂商网络设备适配</span>
          </div>
          <div class="feature-item">
            <span class="feature-dot"></span>
            <span>批量并发作业执行</span>
          </div>
          <div class="feature-item">
            <span class="feature-dot"></span>
            <span>Fernet 凭据加密存储</span>
          </div>
          <div class="feature-item">
            <span class="feature-dot"></span>
            <span>配置版本管理与 Diff</span>
          </div>
        </div>

        <div class="left-footer">
          © 2026 JEM Platform · 自动化 · 智能化
        </div>
      </div>
    </div>

    <!-- Right login card -->
    <div class="login-right">
      <div class="login-card">
        <div class="login-card-header">
          <div class="login-logo">
            <img src="/logo.svg" alt="JEM Logo" class="login-logo-svg" />
          </div>
          <div>
            <div class="login-brand-name">JEM Platform</div>
            <div class="login-brand-sub">控制台 · v2.1</div>
          </div>
        </div>

        <h2 class="login-title">
          <span v-if="mode === 'login'">欢迎回来</span>
          <span v-else-if="mode === 'register'">注册账号</span>
          <span v-else-if="mode === 'forgot'">找回密码</span>
          <span v-else-if="mode === 'reset'">重置密码</span>
        </h2>
        <p class="login-subtitle">
          <span v-if="mode === 'login'">请登录到您的控制台</span>
          <span v-else-if="mode === 'register'">使用邮箱注册，提交后需管理员审核</span>
          <span v-else-if="mode === 'forgot'">输入注册邮箱，我们会发送重置链接</span>
          <span v-else-if="mode === 'reset'">设置您的新密码</span>
        </p>

        <!-- ── 登录 ── -->
        <el-form v-if="mode === 'login'" :model="loginForm" @submit.prevent="login" class="login-form">
          <div class="form-field">
            <label>用户名</label>
            <el-input
              v-model="loginForm.username"
              placeholder="请输入用户名"
              size="large"
              autocomplete="username"
            >
              <template #prefix>
                <el-icon class="field-icon"><User /></el-icon>
              </template>
            </el-input>
          </div>
          <div class="form-field">
            <label>密码</label>
            <el-input
              v-model="loginForm.password"
              type="password"
              placeholder="请输入密码"
              size="large"
              show-password
              autocomplete="current-password"
            >
              <template #prefix>
                <el-icon class="field-icon"><Lock /></el-icon>
              </template>
            </el-input>
          </div>

          <el-button
            type="primary"
            size="large"
            :loading="loading"
            class="login-btn"
            native-type="submit"
          >
            <span v-if="!loading">登录控制台</span>
            <span v-else>登录中...</span>
            <el-icon v-if="!loading" class="btn-arrow"><ArrowRight /></el-icon>
          </el-button>

          <div class="form-links">
            <button type="button" class="link-btn" @click="switchMode('forgot')">忘记密码？</button>
            <span class="link-sep">·</span>
            <button type="button" class="link-btn" @click="switchMode('register')">注册账号</button>
          </div>
        </el-form>

        <!-- ── 注册 ── -->
        <el-form v-else-if="mode === 'register'" :model="registerForm" @submit.prevent="submitRegister" class="login-form">
          <div class="form-field">
            <label>邮箱</label>
            <div class="input-with-action">
              <el-input
                v-model="registerForm.email"
                placeholder="请输入邮箱"
                size="large"
                type="email"
                autocomplete="email"
              />
              <el-button
                size="default"
                :disabled="countdown > 0 || loading"
                :loading="loading && !registerForm.code"
                @click="sendRegisterCode"
                class="code-btn"
              >
                {{ countdown > 0 ? `${countdown}s 后重试` : '获取验证码' }}
              </el-button>
            </div>
          </div>
          <div class="form-field">
            <label>验证码</label>
            <el-input
              v-model="registerForm.code"
              placeholder="请输入 6 位验证码"
              size="large"
              maxlength="6"
            >
              <template #prefix>
                <el-icon class="field-icon"><Key /></el-icon>
              </template>
            </el-input>
          </div>
          <div class="form-field">
            <label>用户名</label>
            <el-input
              v-model="registerForm.username"
              placeholder="2-64 个字符，字母开头"
              size="large"
              autocomplete="username"
            >
              <template #prefix>
                <el-icon class="field-icon"><User /></el-icon>
              </template>
            </el-input>
          </div>
          <div class="form-field">
            <label>姓名（可选）</label>
            <el-input
              v-model="registerForm.full_name"
              placeholder="您的真实姓名"
              size="large"
            />
          </div>
          <div class="form-field">
            <label>密码</label>
            <el-input
              v-model="registerForm.password"
              type="password"
              placeholder="至少 8 位，含大小写字母和数字"
              size="large"
              show-password
              autocomplete="new-password"
            >
              <template #prefix>
                <el-icon class="field-icon"><Lock /></el-icon>
              </template>
            </el-input>
          </div>
          <div class="form-field">
            <label>确认密码</label>
            <el-input
              v-model="registerForm.confirm"
              type="password"
              placeholder="再次输入密码"
              size="large"
              show-password
              autocomplete="new-password"
            >
              <template #prefix>
                <el-icon class="field-icon"><Lock /></el-icon>
              </template>
            </el-input>
          </div>

          <el-button
            type="primary"
            size="large"
            :loading="loading"
            class="login-btn"
            native-type="submit"
          >
            <span v-if="!loading">提交注册</span>
            <span v-else>提交中...</span>
          </el-button>

          <div class="form-links">
            <button type="button" class="link-btn" @click="switchMode('login')">返回登录</button>
          </div>
        </el-form>

        <!-- ── 忘记密码 ── -->
        <el-form v-else-if="mode === 'forgot'" :model="forgotForm" @submit.prevent="submitForgot" class="login-form">
          <div class="form-field">
            <label>注册邮箱</label>
            <el-input
              v-model="forgotForm.email"
              placeholder="请输入注册时使用的邮箱"
              size="large"
              type="email"
              autocomplete="email"
            >
              <template #prefix>
                <el-icon class="field-icon"><Message /></el-icon>
              </template>
            </el-input>
          </div>
          <div class="form-hint">系统会向该邮箱发送密码重置链接，请查收</div>

          <el-button
            type="primary"
            size="large"
            :loading="loading"
            class="login-btn"
            native-type="submit"
          >
            <span v-if="!loading">发送重置链接</span>
            <span v-else>发送中...</span>
          </el-button>

          <div class="form-links">
            <button type="button" class="link-btn" @click="switchMode('login')">返回登录</button>
            <span class="link-sep">·</span>
            <button type="button" class="link-btn" @click="switchMode('register')">注册新账号</button>
          </div>
        </el-form>

        <!-- ── 重置密码 ── -->
        <el-form v-else-if="mode === 'reset'" :model="resetForm" @submit.prevent="submitReset" class="login-form">
          <div v-if="resetTokenValid === false" class="form-error">
            重置链接无效或已过期，请重新获取。
            <button type="button" class="link-btn" @click="switchMode('forgot')">返回重新获取</button>
          </div>
          <template v-else>
            <div class="form-field">
              <label>新密码</label>
              <el-input
                v-model="resetForm.newPassword"
                type="password"
                placeholder="至少 8 位，含大小写字母和数字"
                size="large"
                show-password
                autocomplete="new-password"
              >
                <template #prefix>
                  <el-icon class="field-icon"><Lock /></el-icon>
                </template>
              </el-input>
            </div>
            <div class="form-field">
              <label>确认密码</label>
              <el-input
                v-model="resetForm.confirm"
                type="password"
                placeholder="再次输入新密码"
                size="large"
                show-password
                autocomplete="new-password"
              >
                <template #prefix>
                  <el-icon class="field-icon"><Lock /></el-icon>
                </template>
              </el-input>
            </div>

            <el-button
              type="primary"
              size="large"
              :loading="loading"
              class="login-btn"
              native-type="submit"
            >
              <span v-if="!loading">重置密码</span>
              <span v-else>重置中...</span>
            </el-button>
          </template>

          <div class="form-links">
            <button type="button" class="link-btn" @click="switchMode('login')">返回登录</button>
          </div>
        </el-form>
      </div>

      <div class="right-footer mono">
        Secured · Encrypted · Audited
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  position: relative;
  display: flex;
  height: 100vh;
  width: 100%;
  overflow: hidden;
  background: var(--bg-app);
  isolation: isolate;
}

/* Theme toggle button (top-right) */
.theme-toggle {
  position: absolute;
  top: 18px;
  right: 22px;
  z-index: 10;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 34px;
  padding: 0 14px;
  border: 1px solid var(--border-2);
  background: var(--bg-canvas);
  color: var(--text-1);
  border-radius: 999px;
  cursor: pointer;
  font-size: 12px;
  font-weight: 500;
  letter-spacing: 0.5px;
  transition: all 0.2s ease;
  backdrop-filter: blur(10px);
}
.theme-toggle:hover {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-soft);
  transform: translateY(-1px);
  box-shadow: 0 6px 16px var(--accent-soft);
}

/* ===== Left brand panel ===== */
.login-left {
  flex: 1;
  background: var(--bg-canvas);
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  overflow: hidden;
  border-right: 1px solid var(--border-1);
}

.login-left::before {
  content: '';
  position: absolute;
  inset: 0;
  background:
    radial-gradient(ellipse at 20% 20%, var(--accent-soft), transparent 50%),
    radial-gradient(ellipse at 80% 80%, rgba(124, 58, 237, 0.10), transparent 55%);
  pointer-events: none;
}

.left-content {
  position: relative;
  z-index: 2;
  padding: 60px;
  max-width: 480px;
  width: 100%;
}

.brand-mark {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 36px;
}
.brand-logo {
  width: 72px;
  height: 72px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  filter: drop-shadow(0 8px 24px var(--accent-soft));
}
.brand-logo-svg { width: 100%; height: 100%; }
.brand-text { color: var(--text-1); }
.brand-name-main {
  font-size: 36px;
  font-weight: 900;
  letter-spacing: 3px;
  line-height: 1;
  margin-bottom: 6px;
  background: var(--brand-grad);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  text-shadow: 0 4px 12px var(--accent-soft);
}
.brand-name-sub {
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 1px;
  line-height: 1.4;
  color: var(--text-2);
}

.left-divider {
  width: 48px;
  height: 3px;
  background: var(--brand-grad-2);
  border-radius: 2px;
  margin-bottom: 28px;
}

.left-slogan {
  font-size: 30px;
  font-weight: 800;
  color: var(--text-1);
  line-height: 1.4;
  margin: 0 0 40px;
  letter-spacing: -0.5px;
}

.feature-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
  margin-bottom: 48px;
}
.feature-item {
  display: flex;
  align-items: center;
  gap: 12px;
  color: var(--text-2);
  font-size: 14px;
}
.feature-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
  flex-shrink: 0;
}

.left-footer {
  font-size: 12px;
  color: var(--text-3);
}

/* ===== Right login panel ===== */
.login-right {
  width: 520px;
  min-width: 520px;
  background: var(--bg-app);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px;
  position: relative;
  z-index: 2;
}

.login-card {
  width: 100%;
  max-width: 400px;
  background: var(--bg-canvas);
  border: 1px solid var(--border-2);
  border-radius: var(--radius-lg);
  padding: 36px 32px;
  box-shadow: var(--shadow-3);
  backdrop-filter: blur(20px);
  position: relative;
  overflow: visible;
  max-height: calc(100vh - 80px);
  overflow-y: auto;
}

.login-card::-webkit-scrollbar {
  width: 6px;
}
.login-card::-webkit-scrollbar-thumb {
  background: var(--border-2);
  border-radius: 3px;
}
.login-card::-webkit-scrollbar-thumb:hover {
  background: var(--border-3);
}

.login-card::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 3px;
  background: linear-gradient(90deg, var(--accent), var(--violet), var(--accent));
  opacity: 0.85;
}

.login-card-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 28px;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--border-1);
}
.login-logo {
  width: 48px;
  height: 48px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  padding: 6px;
  background: var(--bg-app);
  border: 1px solid var(--border-2);
  border-radius: var(--radius-md);
}
.login-logo-svg {
  width: 100%;
  height: 100%;
  filter: drop-shadow(0 4px 12px var(--accent-soft));
}
.login-brand-name {
  font-size: 17px;
  font-weight: 800;
  letter-spacing: 1.2px;
  color: var(--text-1);
  background: var(--brand-grad);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
.login-brand-sub {
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-3);
  letter-spacing: 0.6px;
  margin-top: 2px;
}

.login-title {
  font-size: 24px;
  font-weight: 700;
  color: var(--text-1);
  margin: 0 0 6px;
  letter-spacing: -0.3px;
}
.login-subtitle {
  font-size: 13px;
  color: var(--text-3);
  margin: 0 0 26px;
}

.login-form {
  display: flex;
  flex-direction: column;
}

.form-field {
  margin-bottom: 18px;
}
.form-field label {
  display: block;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-2);
  margin-bottom: 8px;
  letter-spacing: 0.4px;
  text-transform: uppercase;
}

/* Refined input styling via :deep */
:deep(.login-form .el-input__wrapper) {
  background: var(--bg-input) !important;
  box-shadow: 0 0 0 1px var(--border-2) inset !important;
  border-radius: var(--radius-md) !important;
  padding: 4px 12px !important;
  transition: box-shadow 0.2s ease, background 0.2s ease !important;
  min-height: 44px !important;
}
:deep(.login-form .el-input__wrapper:hover) {
  box-shadow: 0 0 0 1px var(--border-3) inset !important;
  background: var(--bg-canvas) !important;
}
:deep(.login-form .el-input__wrapper.is-focus) {
  box-shadow: 0 0 0 2px var(--accent) inset, 0 0 0 4px var(--accent-soft) !important;
  background: var(--bg-canvas) !important;
}
:deep(.login-form .el-input__inner) {
  color: var(--text-1) !important;
  font-size: 14px !important;
  height: 22px !important;
}
:deep(.login-form .el-input__inner::placeholder) {
  color: var(--text-3) !important;
}
:deep(.login-form .el-input__prefix) {
  margin-right: 8px;
  color: var(--text-3);
}
:deep(.login-form .el-input.is-focus .el-input__prefix) {
  color: var(--accent);
}
.field-icon {
  font-size: 16px;
  display: inline-flex;
  align-items: center;
}

/* Login button */
.login-btn {
  width: 100%;
  height: 46px !important;
  font-size: 15px !important;
  font-weight: 600 !important;
  letter-spacing: 1px;
  margin-top: 12px;
  background: var(--brand-grad-2) !important;
  border: none !important;
  border-radius: var(--radius-md) !important;
  color: #ffffff !important;
  box-shadow: 0 6px 20px rgba(124, 58, 237, 0.25) !important;
  transition: all 0.2s ease !important;
  display: inline-flex !important;
  align-items: center;
  justify-content: center;
  gap: 8px;
}
.login-btn:hover {
  transform: translateY(-2px) !important;
  box-shadow: 0 10px 28px rgba(124, 58, 237, 0.35) !important;
  filter: brightness(1.1);
}
.login-btn:active {
  transform: translateY(0) !important;
}
.btn-arrow {
  font-size: 16px;
  transition: transform 0.2s ease;
}
.login-btn:hover .btn-arrow {
  transform: translateX(3px);
}

/* Form links row (forgot password / register switcher) */
.form-links {
  margin-top: 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  font-size: 13px;
  color: var(--text-3);
}
.link-btn {
  background: none;
  border: none;
  padding: 4px 6px;
  color: var(--accent);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: opacity 0.2s ease, color 0.2s ease;
  letter-spacing: 0.3px;
}
.link-btn:hover {
  opacity: 0.85;
  text-decoration: underline;
}
.link-sep {
  color: var(--text-4);
  margin: 0 2px;
}

/* Hint / error banners inside forms */
.form-hint {
  font-size: 12px;
  color: var(--text-3);
  margin: -4px 0 12px;
  padding: 0 4px;
  line-height: 1.55;
}
.form-error {
  font-size: 13px;
  color: #ef4444;
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.3);
  padding: 10px 14px;
  border-radius: var(--radius-md);
  margin-bottom: 18px;
  line-height: 1.6;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
}

/* Input + side action (used by 验证码 + 获取验证码) */
.input-with-action {
  display: flex;
  gap: 8px;
  align-items: stretch;
}
.input-with-action .el-input {
  flex: 1;
}
.code-btn {
  flex-shrink: 0;
  height: auto !important;
  white-space: nowrap;
  font-size: 12.5px !important;
  padding: 0 14px !important;
  background: var(--accent-soft) !important;
  color: var(--accent) !important;
  border: 1px solid rgba(34, 211, 238, 0.3) !important;
  border-radius: var(--radius-md) !important;
}
.code-btn:hover:not(:disabled) {
  background: var(--accent) !important;
  color: #fff !important;
  border-color: var(--accent) !important;
}
.code-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.right-footer {
  margin-top: 24px;
  font-size: 10.5px;
  color: var(--text-3);
  letter-spacing: 1.4px;
  text-transform: uppercase;
  text-align: center;
}
</style>
