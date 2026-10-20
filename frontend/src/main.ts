import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'xterm/css/xterm.css'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import App from './App.vue'
import { router } from './router'
import './style.css'
import './assets/theme.css'

// Apply persisted theme before app mount to avoid flash
;(function () {
  try {
    const v = localStorage.getItem('naw.theme')
    const mode = v === 'light' ? 'light' : 'dark'
    document.documentElement.setAttribute('data-theme', mode)
  } catch {
    document.documentElement.setAttribute('data-theme', 'dark')
  }
})()

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(ElementPlus)
for (const [key, comp] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, comp as never)
}
app.mount('#app')

// ── Browser-extension noise filter ────────────────────────────────────
// Browser extensions (MetaMask, OKX, Phantom, …) inject their own
// React + MobX runtimes into the page; when those extensions
// render an in-overlay pop-up they occasionally throw React errors
// like "Objects are not valid as a React child" that surface in
// DevTools as if they came from this app — they did not. The
// extension runs in an isolated world but throws against the page's
// ``window``, so Vue's own handler doesn't see them.
//
// We tag those errors once so operators triaging DevTools can
// distinguish "extension noise" from "real NAW bug" without
// trawling the stack. Real NAW errors are reported untouched.
;(function markExtensionNoise() {
  if (typeof window === 'undefined') return
  window.addEventListener(
    'error',
    (ev) => {
      const src = String(ev?.filename || '')
      const msg = String(ev?.message || '')
      if (src.startsWith('chrome-extension://') ||
          src.startsWith('moz-extension://') ||
          src.startsWith('safari-web-extension://') ||
          msg.includes('Objects are not valid as a React child')) {
        // Tag the message so the operator sees a clear marker the
        // first time it happens in a session.
        const marker = '[extension-noise]'
        if (!ev.message?.startsWith?.(marker)) {
          // eslint-disable-next-line no-console
          console.info(
            marker,
            'Above error comes from a browser extension (e.g. MetaMask), not from NAW. Safe to ignore.',
            { source: src },
          )
        }
      }
    },
    true,
  )
})()
