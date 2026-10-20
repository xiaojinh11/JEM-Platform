<script setup lang="ts">
import { useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useThemeStore } from '@/stores/theme'
import AppLayout from '@/components/AppLayout.vue'

const route = useRoute()

// Importing the stores (without storing the returned refs) is enough to
// ensure their reactive watchers / state initialise on app boot. We do
// NOT call /auth/me here on purpose — doing so on cold page load fires
// the 401 interceptor immediately if the stored JWT is stale, popping
// the "会话过期" dialog before the user has even seen the login form.
// Token validation is driven lazily by individual pages (router guard
// + auth.fetchMe) instead.
useAuthStore()
useThemeStore()
</script>

<template>
  <div class="app-root">
    <template v-if="route.meta.layout === 'plain'">
      <router-view />
    </template>
    <template v-else>
      <AppLayout>
        <!--
          Keep-alive the entire authenticated layout so navigating away
          from the WebSSH / multi-shell page and back doesn't destroy
          the live SSH sessions, terminal buffers, or in-flight log
          state.  Long-running terminal sessions (especially over NAT
          keepalives that may have already dropped) survive the route
          transition; the WebSocket itself is owned by the browser and
          is not torn down when the component is merely deactivated.

          Important: Vue 3 ``<keep-alive>`` only caches components it
          can identify.  When wrapping ``<router-view />`` directly,
          the dynamic component inside the slot has no stable name for
          the cache to key on, so every navigation effectively
          unmounts and remounts the page — which is exactly the bug
          the user reported as "切走再切回终端空白".

          The fix is the standard router-view + keep-alive pattern:
          expose the resolved component via ``v-slot``, pass it to
          ``<component :is>``, and let ``<keep-alive>`` see the
          actual page component (which has ``defineOptions({ name })``).
          The ``include`` list matches those names so only the pages
          we want to cache are cached.
        -->
        <router-view v-slot="{ Component }">
          <!--
            Only MultiShellPage currently needs keep-alive (it owns
            long-lived WebSocket SSH sessions + xterm canvas state).
            Other pages are remounted on every navigation — they're
            cheap to recreate and their state isn't worth caching.
            The :include list uses the `name` declared in each
            page's `defineOptions({ name })`.
          -->
          <keep-alive :include="['MultiShellPage']">
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </AppLayout>
    </template>
  </div>
</template>

<style>
/* Theme tokens (sets :root data-theme vars) */
@import '@/assets/theme.css';

.app-root {
  height: 100%;
  width: 100%;
  background: var(--bg-app);
  color: var(--text-1);
}

.page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  background: var(--bg-canvas);
  border: 1px solid var(--border-1);
  border-radius: var(--radius-md);
}
.page-title {
  font-family: var(--font-mono);
  font-size: 13px;
  font-weight: 700;
  color: var(--text-1);
  letter-spacing: 0.6px;
  text-transform: uppercase;
  padding-left: 4px;
}
.page-title::before {
  content: '';
  display: inline-block;
  width: 3px;
  height: 14px;
  background: var(--accent);
  margin-right: 8px;
  vertical-align: -2px;
}

.mono { font-family: var(--font-mono); font-size: 12.5px; }
.muted { color: var(--text-4); }

::selection {
  background: var(--accent-soft);
  color: var(--accent);
}
</style>

<style scoped>
.app-root { height: 100%; }
</style>
