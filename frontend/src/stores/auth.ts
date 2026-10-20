import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api, type LoginRequest, type TokenResponse } from '@/api'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(localStorage.getItem('naw.token'))
  const user = ref<Record<string, unknown> | null>(null)
  /**
   * Set when a 401 forces sign-out. Lets the http client and the router
   * coordinate without re-entering ``logout()`` from multiple places.
   */
  const sessionExpired = ref(false)

  async function login(payload: LoginRequest): Promise<TokenResponse> {
    const t = await api.login(payload)
    token.value = t.access_token
    localStorage.setItem('naw.token', t.access_token)
    sessionExpired.value = false
    user.value = await api.me()
    return t
  }

  async function fetchMe() {
    if (!token.value) return null
    try {
      user.value = await api.me()
      return user.value
    } catch (err) {
      // The stored JWT is stale (signed with an older SECRET_KEY, or
      // expired). The http 401 interceptor will also fire — but we
      // want a deterministic behaviour here: clear the bad token and
      // route the user to /login so the next attempt starts fresh,
      // instead of leaving them on a page that will keep 401-ing on
      // every subsequent fetch.
      clearSession('expired')
      const hash = location.hash
      if (!hash.startsWith('#/login')) {
        location.hash = '#/login'
      }
      throw err
    }
  }

  /**
   * Atomically clear the session and flag the cause.
   *
   * Called from the http 401 interceptor (the request path) and from the
   * logout button (user-initiated). Centralising the cleanup avoids the
   * previous bug where ``api.ts`` deleted localStorage but never reset the
   * Pinia ref, so router guards saw a stale ``token`` and bounced the
   * user back to dashboard after redirecting to /login.
   */
  function clearSession(reason: 'logout' | 'expired' = 'logout') {
    token.value = null
    user.value = null
    localStorage.removeItem('naw.token')
    sessionExpired.value = reason === 'expired'
  }

  function logout() {
    clearSession('logout')
    if (location.hash !== '#/login') {
      location.hash = '#/login'
    }
  }

  /**
   * Called from the http 401 handler. Idempotent — repeated 401s while
   * the user is mid-redirect won't queue up multiple hash assignments.
   *
   * IMPORTANT: we no longer auto-redirect to /login here. The 401
   * interceptor in api.ts owns the UX decision (popup-confirm vs auto
   * redirect) so it can avoid the previous bug where every random 401
   * (e.g. /ai/config/stats before the first AI config save) kicked the
   * user out mid-flow. ``expireSession`` now just clears Pinia + storage
   * so the next request is treated as anonymous; the redirect happens
   * only when the user clicks the confirmation button.
   */
  function expireSession() {
    if (token.value === null && !sessionExpired.value) return
    clearSession('expired')
  }

  return { token, user, sessionExpired, login, logout, fetchMe, clearSession, expireSession }
})
