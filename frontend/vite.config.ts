import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import react from '@vitejs/plugin-react'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [
    vue(),
    react(),
    AutoImport({
      imports: ['vue', 'vue-router', 'pinia'],
      resolvers: [ElementPlusResolver()],
      dts: 'src/auto-imports.d.ts',
    }),
    Components({
      resolvers: [ElementPlusResolver()],
      dts: 'src/components.d.ts',
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    host: '0.0.0.0',
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:18000',
        changeOrigin: true,
      },
      // WebSocket upgrades for /api/v1/ws/* must use the ws proxy so
      // that the HTTP → WS upgrade handshake is forwarded correctly.
      // The bare /ws rule above is kept for any other WS paths.
      '/api/v1/ws': {
        target: 'ws://127.0.0.1:18000',
        ws: true,
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://127.0.0.1:18000',
        ws: true,
      },
    },
  },
  esbuild: {
    // Monaco-Editor 0.5x ships with a buggy .map reference (it points
    // at a .umd.js.map file that does not exist alongside the .js
    // file Vite actually loads).  Vite would otherwise log a
    // `Failed to load source map` warning on every page load.  The
    // warning is purely cosmetic — the JS itself is fine — but
    // it spams the dev console and looks like an error.
    //
    // We tell esbuild not to emit sourcemap-related warnings for
    // files inside node_modules.  The downside is we lose Monaco
    // source maps in dev; the upside is a clean console.  This is
    // the same trade-off Vite users on the Monaco project
    // recommend on the project's issue tracker.
    logOverride: { 'sourcemap-not-found': 'silent' },
  },
  optimizeDeps: {
    // Don't pre-bundle Monaco — it self-loads via AMD loader below
    exclude: ['monaco-editor'],
  },
  build: {
    chunkSizeWarningLimit: 1500, // Monaco chunks are large
    rollupOptions: {
      output: {
        manualChunks: {
          'monaco-core': ['monaco-editor/esm/vs/editor/editor.api'],
        },
      },
    },
  },
})
