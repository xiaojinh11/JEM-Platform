import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

export type ThemeMode = 'dark' | 'light'
const STORAGE_KEY = 'naw.theme'

function readInitial(): ThemeMode {
  if (typeof localStorage === 'undefined') return 'dark'
  const v = localStorage.getItem(STORAGE_KEY)
  if (v === 'dark' || v === 'light') return v
  return 'dark'
}

export const useThemeStore = defineStore('theme', () => {
  const theme = ref<ThemeMode>(readInitial())

  function apply(mode: ThemeMode) {
    document.documentElement.setAttribute('data-theme', mode)
  }

  // Apply on init
  apply(theme.value)

  // Persist + apply on change
  watch(theme, (m) => {
    apply(m)
    localStorage.setItem(STORAGE_KEY, m)
  })

  function toggle() {
    theme.value = theme.value === 'dark' ? 'light' : 'dark'
  }

  function set(m: ThemeMode) {
    theme.value = m
  }

  return { theme, toggle, set }
})
