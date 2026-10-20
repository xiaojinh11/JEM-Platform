import { createRouter, createWebHashHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const routes = [
  { path: '/login', component: () => import('@/pages/LoginPage.vue'), meta: { layout: 'plain' } },
  { path: '/', redirect: '/shell' },
  {
    path: '/dashboard',
    component: () => import('@/pages/DashboardPage.vue'),
    meta: { title: '全网概况' },
  },
  {
    path: '/devices',
    component: () => import('@/pages/DevicesPage.vue'),
    meta: { title: '设备台账' },
  },
  {
    path: '/devices/:id(\\d+)',
    component: () => import('@/pages/DeviceDetailPage.vue'),
    meta: { title: '设备详情' },
  },
  {
    path: '/devices/:id(\\d+)/shell',
    redirect: (to) => ({ path: '/shell', query: { devices: String(to.params.id) } }),
    meta: { title: 'WebSSH' },
  },
  {
    path: '/shell',
    component: () => import('@/pages/MultiShellPage.vue'),
    meta: { title: 'WebSSH · 多设备终端' },
  },
  {
    path: '/configs',
    component: () => import('@/pages/ConfigsPage.vue'),
    meta: { title: '配置中心' },
  },
  {
    path: '/commands',
    component: () => import('@/pages/CommandCollectPage.vue'),
    meta: { title: '命令采集' },
  },
  {
    path: '/documents',
    component: () => import('@/pages/DocumentsPage.vue'),
    meta: { title: '手册库' },
  },
  {
    path: '/documents/:id(\\d+)/offline',
    component: () => import('@/pages/OfflineManualPage.vue'),
    meta: { title: '原生离线手册' },
  },
  {
    path: '/_selftest/viewer',
    component: () => import('@/pages/DocxViewerSelfTest.vue'),
    meta: { title: 'DocxViewer 自检', layout: 'plain' },
  },
  {
    path: '/jobs',
    component: () => import('@/pages/JobsPage.vue'),
    meta: { title: '批量任务' },
  },
  {
    path: '/audit',
    component: () => import('@/pages/AuditPage.vue'),
    meta: { title: '审计日志' },
  },
  {
    path: '/settings',
    component: () => import('@/pages/SettingsPage.vue'),
    meta: { title: '系统设置' },
  },
  {
    path: '/drift',
    component: () => import('@/pages/DriftPage.vue'),
    meta: { title: '配置漂移' },
  },
  {
    path: '/visualization',
    component: () => import('@/pages/VisualizationPage.vue'),
    meta: { title: '关系图谱' },
  },
  {
    path: '/ai',
    component: () => import('@/pages/AIAssistantPage.vue'),
    meta: { title: 'AI 助手' },
  },
]

export const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  // Re-sync the Pinia ref with the latest localStorage value on every
  // navigation. The auth store only reads localStorage once at module
  // init, so a 401 triggered ``clearSession`` could leave later
  // navigations seeing a stale value. Reading here is cheap (local
  // storage is sync) and prevents the half-cleared state where the
  // Pinia ``user`` fallback to ``'admin'`` shows the avatar but the
  // token ref is already null.
  const stored = localStorage.getItem('naw.token')
  if (stored !== auth.token) {
    auth.token = stored
  }
  if (!auth.token && to.path !== '/login') {
    return { path: '/login' }
  }
  if (auth.token && to.path === '/login') {
    return { path: '/dashboard' }
  }

  // The token is in localStorage but the user profile hasn't been
  // fetched yet (page refresh, or tab opened directly). Without this
  // the layout would show "未登录" until some other component happens
  // to call fetchMe. Loading it here once is enough — the ref stays
  // populated for the rest of the session.
  if (auth.token && !auth.user) {
    try {
      await auth.fetchMe()
    } catch {
      // fetchMe already cleared the session + redirected to /login on
      // failure; nothing more to do here.
    }
  }
})
