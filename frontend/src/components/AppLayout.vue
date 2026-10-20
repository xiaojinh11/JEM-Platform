<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useThemeStore } from '@/stores/theme'
import { VENDOR_LINKS, type VendorLink } from '@/constants/documents'
import ParticleBackground from '@/components/ParticleBackground.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const theme = useThemeStore()

const activePath = computed(() => route.path)
// ``isLoggedIn`` keys off the token (which is restored from localStorage
// synchronously), not the ``user`` ref — the latter is fetched lazily
// by the router guard. Otherwise the layout shows "未登录" for the first
// ~200 ms after a hard refresh even though the session is valid.
const isLoggedIn = computed(() => !!auth.token)
const username   = computed(() => (auth.user as { username?: string } | null)?.username || '加载中…')
const initials   = computed(() => String(username.value).slice(0, 2).toUpperCase())
const role       = computed(() => (auth.user as { role?: string } | null)?.role || 'guest')

const roleLabel = computed(() => {
  if (role.value === 'admin') return 'ADMIN'
  if (role.value === 'operator') return 'OP'
  if (role.value === 'guest') return 'OFFLINE'
  return 'READONLY'
})

const navItems = [
  { path: '/dashboard', title: '全网概况', icon: 'DataBoard' },
  { path: '/shell',     title: 'WebSSH · Console', icon: 'Monitor' },
  { path: '/ai',        title: 'AI 助手',   icon: 'MagicStick' },
  { path: '/drift',     title: '配置漂移', icon: 'WarningFilled' },
  { path: '/devices',   title: '设备台账', icon: 'Cpu' },
  { path: '/configs',   title: '配置中心', icon: 'Files' },
  { path: '/jobs',      title: '批量任务', icon: 'Grid' },
  { path: '/documents', title: '手册库',   icon: 'Notebook' },
  { path: '/visualization', title: '关系图谱', icon: 'Share' },
  { path: '/audit',     title: '审计日志', icon: 'Key' },
  { path: '/settings',  title: '系统设置', icon: 'Tools' },
]

const pageTitle = computed(() => {
  const item = navItems.find((i) => route.path.startsWith(i.path))
  return item?.title || '工作台'
})

const topbarQuery = ref('')
const onTopbarSearch = () => {
  if (topbarQuery.value.trim()) {
    router.push({ path: '/devices', query: { q: topbarQuery.value.trim() } })
    topbarQuery.value = ''
  }
}

const now = ref(new Date())
setInterval(() => { now.value = new Date() }, 1000)
const clockText = computed(() => now.value.toLocaleString('zh-CN', {
  hour12: false, year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
}))

const isLight = computed(() => theme.theme === 'light')

// Vendor quick-links grouped by category for the sidebar panel.
// `网络` is the bigger group so we surface it first.
const vendorGroups = computed<{ label: string; items: VendorLink[] }[]>(() => {
  const order: VendorLink['group'][] = ['网络', '安全']
  const buckets = new Map<VendorLink['group'], VendorLink[]>()
  for (const v of VENDOR_LINKS) {
    const arr = buckets.get(v.group) ?? []
    arr.push(v)
    buckets.set(v.group, arr)
  }
  return order
    .filter((g) => buckets.has(g))
    .map((g) => ({ label: g, items: buckets.get(g)! }))
})

// Open an external vendor site in a new tab.  Pulled into a function so
// we can guard with `rel="noopener noreferrer"` from the template.
const openVendor = (url: string) => {
  window.open(url, '_blank', 'noopener,noreferrer')
}

// ── Vendor drawer ──
// The drawer is closed by default; clicking the sidebar entry opens it.
// Within the drawer the user can search by vendor name (Chinese, pinyin,
// or URL hostname) and open the official site in a new tab.
const vendorDrawerOpen = ref(false)
const vendorSearch = ref('')
const filteredVendorGroups = computed(() => {
  const q = vendorSearch.value.trim().toLowerCase()
  if (!q) return vendorGroups.value
  return vendorGroups.value
    .map((g) => ({
      label: g.label,
      items: g.items.filter((v) => {
        // naive multi-field match: name + url host.  We don't ship a
        // pinyin table; users can still search by Latin chars.
        const host = (() => {
          try { return new URL(v.url).hostname.replace(/^www\./, '') } catch { return v.url }
        })()
        return v.name.toLowerCase().includes(q) || host.toLowerCase().includes(q)
      }),
    }))
    .filter((g) => g.items.length > 0)
})
const totalVendorCount = computed(() =>
  VENDOR_LINKS.length,
)
const openVendorDrawer = () => {
  vendorDrawerOpen.value = true
}

// ── Contact help dialog ──
// Triggered by the "?" icon in the topbar. Surfaces the contact
// details so users can reach the operator for support.
const contactOpen = ref(false)
</script>

<template>
  <div class="layout">
    <!-- ===== Sidebar ===== -->
    <aside class="sidebar">
      <div class="sidebar-brand">
        <div class="brand-logo-slot">
          <img src="/logo.svg" alt="JEM Logo" class="brand-logo-img" />
        </div>
        <div class="brand-meta">
          <div class="brand-name">JEM Platform</div>
          <div class="brand-sub">v1.5 · console</div>
        </div>
      </div>

      <nav class="sidebar-nav">
        <router-link
          v-for="item in navItems"
          :key="item.path"
          :to="item.path"
          class="nav-item"
          :class="{ active: activePath.startsWith(item.path) }"
        >
          <el-icon class="nav-icon"><component :is="item.icon" /></el-icon>
          <span class="nav-text">{{ item.title }}</span>
          <span
            v-if="item.path === '/jobs'"
            class="nav-tag"
          >PIPE</span>
        </router-link>
      </nav>

      <!-- ── Vendor quick-links trigger ──
           The vendor list is folded into a button by default; clicking
           opens a right-side drawer with the full searchable grid. -->
      <button
        type="button"
        class="vendor-trigger"
        :class="{ open: vendorDrawerOpen }"
        @click="openVendorDrawer"
      >
        <el-icon class="trigger-icon"><Promotion /></el-icon>
        <span class="trigger-text">厂商直达</span>
        <span class="trigger-count">{{ totalVendorCount }}</span>
        <el-icon class="trigger-arrow"><ArrowRight /></el-icon>
      </button>

      <div class="sidebar-footer">
        <div class="user-card">
          <div class="user-avatar">{{ initials }}</div>
          <div class="user-info">
            <div class="user-name">{{ username }}</div>
            <div class="user-role">{{ roleLabel }}</div>
          </div>
        </div>
      </div>
    </aside>

    <!-- ===== Main area ===== -->
    <div class="main-area">
      <header class="topbar">
        <div class="topbar-left">
          <div class="page-title">{{ pageTitle }}</div>
        </div>

        <div class="topbar-center">
          <el-input
            v-model="topbarQuery"
            placeholder="搜索设备 / IP / SN"
            size="default"
            clearable
            class="topbar-search"
            @keyup.enter="onTopbarSearch"
          >
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>
        </div>

        <div class="topbar-right">
          <div class="clock mono">{{ clockText }}</div>

          <el-tooltip :content="isLight ? '切换到黑夜模式' : '切换到白天模式'" placement="bottom">
            <button class="icon-btn theme-btn" @click="theme.toggle()" :aria-label="isLight ? '切换到黑夜' : '切换到白天'">
              <el-icon v-if="isLight"><Moon /></el-icon>
              <el-icon v-else><Sunny /></el-icon>
            </button>
          </el-tooltip>

          <el-tooltip content="联系我" placement="bottom">
            <button class="icon-btn" @click="contactOpen = true">
              <el-icon><QuestionFilled /></el-icon>
            </button>
          </el-tooltip>

          <el-dropdown trigger="click">
            <div class="user-btn">
              <div class="user-avatar-sm">{{ initials }}</div>
              <span class="user-name-sm">{{ username }}</span>
              <el-icon><ArrowDown /></el-icon>
            </div>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="router.push('/settings')">系统设置</el-dropdown-item>
                <el-dropdown-item divided @click="auth.logout()">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>

      <main class="content">
        <!-- Behind every authenticated page.
             Calmer than the login screen: the page content (tables, cards,
             sidebars) has to win the user's attention. -->
        <ParticleBackground :density="40" :speed="0.18" :link-distance="100" :mouse-radius="180" :flow-strength="0.12" />
        <div class="content-inner">
          <slot />
        </div>
      </main>
    </div>

    <!-- ── Vendor drawer ──
         Right-side panel that pops up when the user clicks the
         "厂商直达" trigger in the sidebar.  Lays out all vendor links
         as cards grouped by 网络 / 安全, with live search. -->
    <el-drawer
      v-model="vendorDrawerOpen"
      direction="rtl"
      size="440px"
      :with-header="false"
      modal-class="vendor-drawer-mask"
      class="vendor-drawer"
    >
      <div class="vd-head">
        <div class="vd-title">
          <el-icon class="vd-title-icon"><Promotion /></el-icon>
          <div>
            <div class="vd-title-main">厂商直达</div>
            <div class="vd-title-sub">点击任意卡片跳转至厂商官方网站</div>
          </div>
        </div>
        <el-input
          v-model="vendorSearch"
          placeholder="按名称 / 域名搜索"
          clearable
          class="vd-search"
        >
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
      </div>

      <div class="vd-body">
        <template v-if="filteredVendorGroups.length">
          <section
            v-for="g in filteredVendorGroups"
            :key="g.label"
            class="vd-group"
          >
            <div class="vd-group-label">
              <span class="vd-group-dot" :class="`vd-group-${g.label}`"></span>
              <span>{{ g.label }}</span>
              <span class="vd-group-count">{{ g.items.length }}</span>
            </div>
            <div class="vd-grid">
              <button
                v-for="v in g.items"
                :key="v.name"
                type="button"
                class="vd-card"
                @click="openVendor(v.url)"
              >
                <div class="vd-card-head">
                  <span class="vd-card-name">{{ v.name }}</span>
                  <el-icon class="vd-card-ext"><TopRight /></el-icon>
                </div>
                <div class="vd-card-url">{{ v.url }}</div>
              </button>
            </div>
          </section>
        </template>
        <el-empty
          v-else
          description="没有匹配的厂商"
          :image-size="80"
          class="vd-empty"
        />
      </div>

      <div class="vd-foot">
        <span>共 {{ totalVendorCount }} 家厂商 · 点击在新标签页打开</span>
      </div>
    </el-drawer>

    <!-- ── Contact help dialog ──
         Triggered by the topbar "?" icon. Shows the operator's QQ
         and email so users can reach out for support. -->
    <el-dialog
      v-model="contactOpen"
      title="咨询帮助 · 请联系我"
      width="420px"
      align-center
      class="contact-dialog"
    >
      <div class="contact-body">
        <div class="contact-hint">
          使用中遇到问题、有功能建议或合作意向，欢迎通过以下方式联系：
        </div>
        <div class="contact-row">
          <div class="contact-label">
            <el-icon><ChatDotRound /></el-icon>
            <span>QQ</span>
          </div>
          <a class="contact-value mono" href="https://wpa.qq.com/msgrd?v=3&uin=1397652391&site=qq&menu=yes" target="_blank" rel="noopener noreferrer">1397652391</a>
        </div>
        <div class="contact-row">
          <div class="contact-label">
            <el-icon><Message /></el-icon>
            <span>邮箱</span>
          </div>
          <a class="contact-value mono" href="mailto:liuyan357514@hotmail.com">liuyan357514@hotmail.com</a>
        </div>
      </div>
      <template #footer>
        <el-button type="primary" @click="contactOpen = false">知道了</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  height: 100vh;
  width: 100%;
  overflow: hidden;
  background: var(--bg-app);
}

/* ===== Sidebar ===== */
.sidebar {
  width: 220px;
  min-width: 220px;
  background: var(--bg-canvas);
  border-right: 1px solid var(--border-1);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  position: relative;
  z-index: 2;
}

.sidebar-brand {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 18px 18px 14px;
  border-bottom: 1px solid var(--border-1);
}
.brand-logo-slot {
  width: 36px;
  height: 36px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 4px;
  background: var(--bg-app);
  border: 1px solid var(--border-2);
  border-radius: var(--radius-md);
  filter: drop-shadow(0 4px 10px var(--accent-soft));
}
.brand-logo-img { width: 100%; height: 100%; }
.brand-name {
  font-size: 13px;
  font-weight: 800;
  letter-spacing: 1.2px;
  background: var(--brand-grad);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
.brand-sub {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-3);
  letter-spacing: 0.6px;
  margin-top: 3px;
}

.sidebar-nav {
  flex: 1 1 auto;
  min-height: 0;
  padding: 10px 8px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 1px;
}
/* ── Vendor trigger (folded state) ──
   Sits between the main nav and the user card.  Acts like a nav item:
   hover/active animations match the existing nav-item style. */
.vendor-trigger {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 4px 8px 6px;
  padding: 9px 12px;
  border-radius: var(--radius-sm);
  color: var(--text-2);
  font-size: 13px;
  background: transparent;
  border: 1px solid transparent;
  cursor: pointer;
  font-family: inherit;
  text-align: left;
  transition: all 0.15s ease;
  position: relative;
}
.vendor-trigger .trigger-icon {
  font-size: 15px;
  color: var(--accent);
  flex-shrink: 0;
}
.vendor-trigger .trigger-text { flex: 1; }
.vendor-trigger .trigger-count {
  font-family: var(--font-mono);
  font-size: 10px;
  letter-spacing: 0.4px;
  color: var(--text-3);
  background: var(--bg-app);
  border: 1px solid var(--border-1);
  border-radius: 8px;
  padding: 0 6px;
  line-height: 16px;
  height: 16px;
  flex-shrink: 0;
}
.vendor-trigger .trigger-arrow {
  font-size: 11px;
  color: var(--text-3);
  transition: transform 0.18s ease, color 0.18s ease;
}
.vendor-trigger:hover {
  background: var(--bg-elev);
  color: var(--text-1);
  border-color: var(--border-1);
}
.vendor-trigger:hover .trigger-arrow {
  color: var(--accent);
  transform: translateX(2px);
}
.vendor-trigger.open {
  background: var(--accent-soft);
  color: var(--accent);
  border-color: rgba(34, 211, 238, 0.25);
}
.vendor-trigger.open .trigger-arrow { color: var(--accent); }

/* ── Vendor drawer ──
   Element Plus drawer host: keep our own header/body/footer so we can
   design freely instead of using the default EP header. */
.vendor-drawer :deep(.el-drawer__body) {
  padding: 0;
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg-canvas);
}
.vd-head {
  padding: 18px 20px 14px;
  border-bottom: 1px solid var(--border-1);
  background: linear-gradient(180deg, var(--bg-elev) 0%, var(--bg-canvas) 100%);
  flex-shrink: 0;
}
.vd-title {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}
.vd-title-icon {
  font-size: 22px;
  color: var(--accent);
  background: var(--accent-soft);
  border: 1px solid rgba(34, 211, 238, 0.3);
  padding: 8px;
  border-radius: var(--radius-sm);
}
.vd-title-main {
  font-size: 15px;
  font-weight: 700;
  color: var(--text-1);
  letter-spacing: 0.4px;
}
.vd-title-sub {
  font-size: 11.5px;
  color: var(--text-3);
  margin-top: 2px;
  letter-spacing: 0.2px;
}
.vd-search :deep(.el-input__wrapper) {
  background: var(--bg-app);
  box-shadow: 0 0 0 1px var(--border-2) inset;
  border-radius: var(--radius-sm);
}
.vd-search :deep(.el-input__wrapper.is-focus) {
  box-shadow: 0 0 0 1px var(--accent) inset;
}
.vd-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px 20px;
}
.vd-body::-webkit-scrollbar { width: 8px; }
.vd-body::-webkit-scrollbar-track { background: transparent; }
.vd-body::-webkit-scrollbar-thumb {
  background: var(--border-2);
  border-radius: 4px;
}
.vd-body::-webkit-scrollbar-thumb:hover { background: var(--border-3); }
.vd-group { margin-bottom: 18px; }
.vd-group-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-family: var(--font-mono);
  font-size: 10.5px;
  letter-spacing: 1px;
  color: var(--text-3);
  text-transform: uppercase;
  margin-bottom: 8px;
  padding: 0 2px;
}
.vd-group-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  display: inline-block;
}
.vd-group-dot.vd-group-网络 { background: var(--accent); box-shadow: 0 0 6px var(--accent); }
.vd-group-dot.vd-group-安全 { background: #f97316; box-shadow: 0 0 6px #f97316; }
.vd-group-count {
  margin-left: auto;
  font-size: 10px;
  color: var(--text-3);
  background: var(--bg-app);
  padding: 1px 7px;
  border-radius: 8px;
  border: 1px solid var(--border-1);
}
.vd-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.vd-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px 12px 11px;
  background: var(--bg-app);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
  cursor: pointer;
  text-align: left;
  font-family: inherit;
  transition: all 0.18s ease;
  position: relative;
  overflow: hidden;
  min-width: 0;
}
.vd-card::before {
  content: '';
  position: absolute;
  left: 0; top: 0; bottom: 0;
  width: 2px;
  background: transparent;
  transition: background 0.18s ease;
}
.vd-card .vd-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}
.vd-card .vd-card-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-1);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}
.vd-card .vd-card-ext {
  font-size: 11px;
  color: var(--text-3);
  opacity: 0;
  transform: translate(-2px, 2px);
  transition: all 0.18s ease;
  flex-shrink: 0;
}
.vd-card .vd-card-url {
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-3);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  direction: rtl;
  text-align: left;
}
.vd-card:hover {
  border-color: rgba(34, 211, 238, 0.45);
  background: var(--accent-soft);
  transform: translateY(-1px);
  box-shadow: 0 6px 16px -8px rgba(34, 211, 238, 0.4);
}
.vd-card:hover::before { background: var(--accent); }
.vd-card:hover .vd-card-ext {
  opacity: 1;
  transform: translate(0, 0);
  color: var(--accent);
}
.vd-card:hover .vd-card-url { color: var(--accent); }
.vd-card:active { transform: translateY(0); }
.vd-empty { padding: 60px 0; }
.vd-foot {
  padding: 10px 20px;
  border-top: 1px solid var(--border-1);
  background: var(--bg-elev);
  font-family: var(--font-mono);
  font-size: 10.5px;
  color: var(--text-3);
  letter-spacing: 0.4px;
  flex-shrink: 0;
}

/* ── Contact help dialog ── */
.contact-dialog :deep(.el-dialog__header) {
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border-1);
  margin-right: 0;
}
.contact-body {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 4px 2px;
}
.contact-hint {
  font-size: 13px;
  color: var(--text-2);
  line-height: 1.6;
  padding: 10px 12px;
  background: var(--accent-soft);
  border: 1px solid rgba(34, 211, 238, 0.25);
  border-radius: var(--radius-sm);
}
.contact-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 12px 14px;
  background: var(--bg-app);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
}
.contact-label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text-2);
  font-weight: 600;
}
.contact-label .el-icon {
  font-size: 16px;
  color: var(--accent);
}
.contact-value {
  font-size: 14px;
  color: var(--accent);
  font-weight: 600;
  text-decoration: none;
  letter-spacing: 0.4px;
  transition: opacity 0.18s ease;
}
.contact-value:hover {
  opacity: 0.8;
  text-decoration: underline;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border-radius: var(--radius-sm);
  color: var(--text-2);
  font-size: 13px;
  text-decoration: none;
  transition: all 0.15s ease;
  position: relative;
  cursor: pointer;
}
.nav-item .nav-icon { font-size: 15px; flex-shrink: 0; }
.nav-item .nav-text { flex: 1; }
.nav-item .nav-tag {
  font-family: var(--font-mono);
  font-size: 9px;
  letter-spacing: 0.6px;
  color: var(--accent);
  border: 1px solid rgba(34,211,238,0.3);
  padding: 0 4px;
  border-radius: 2px;
  line-height: 14px;
  height: 14px;
}
.nav-item:hover {
  background: var(--bg-elev);
  color: var(--text-1);
}
.nav-item.active {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 500;
}
.nav-item.active::before {
  content: '';
  position: absolute;
  left: 0; top: 6px; bottom: 6px;
  width: 2px;
  background: var(--accent);
  border-radius: 0 2px 2px 0;
}

.sidebar-footer {
  flex: 0 0 auto;
  padding: 12px;
  border-top: 1px solid var(--border-1);
  background: var(--bg-canvas);
}
.user-card {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  background: var(--bg-canvas);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
}
.user-avatar {
  width: 28px;
  height: 28px;
  border-radius: var(--radius-sm);
  background: var(--accent-soft);
  color: var(--accent);
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
}
.user-info { line-height: 1.3; }
.user-name {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-1);
}
.user-role {
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-3);
  letter-spacing: 0.6px;
}

/* ===== Main area ===== */
.main-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 0;
  position: relative;
}

.topbar {
  height: 48px;
  min-height: 48px;
  background: var(--bg-canvas);
  border-bottom: 1px solid var(--border-1);
  display: flex;
  align-items: center;
  padding: 0 18px;
  gap: 16px;
  flex-shrink: 0;
  position: relative;
  z-index: 3;
}
.topbar-left { flex-shrink: 0; }

.topbar-center { flex: 1; max-width: 360px; }
.topbar-search :deep(.el-input__wrapper) {
  background: var(--bg-input);
  border-radius: var(--radius-sm);
  box-shadow: 0 0 0 1px var(--border-2) inset;
}
.topbar-search :deep(.el-input__wrapper.is-focus) {
  box-shadow: 0 0 0 1px var(--accent) inset;
}

.topbar-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 10px;
}
.clock {
  color: var(--text-2);
  font-size: 12px;
  letter-spacing: 0.4px;
}
.icon-btn {
  width: 32px;
  height: 32px;
  border: 1px solid var(--border-1);
  background: var(--bg-canvas);
  border-radius: var(--radius-sm);
  cursor: pointer;
  color: var(--text-2);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
  transition: all 0.18s ease;
}
.icon-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-soft);
}
.theme-btn:hover {
  transform: rotate(15deg);
}
.user-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px 4px 4px;
  border: 1px solid var(--border-1);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all 0.15s;
}
.user-btn:hover { border-color: var(--border-3); }
.user-avatar-sm {
  width: 22px;
  height: 22px;
  border-radius: var(--radius-sm);
  background: var(--accent-soft);
  color: var(--accent);
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
}
.user-name-sm {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-1);
}

.content {
  flex: 1;
  overflow-y: auto;
  position: relative;
  /* keep particle behind content */
}
.content-inner {
  position: relative;
  z-index: 1;
  padding: 16px 18px;
  min-height: 100%;
}
</style>
