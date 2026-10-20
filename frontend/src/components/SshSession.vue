<script setup lang="ts">
/**
 * One SSH session = one xterm + one websocket + one command-log.
 *
 * The right pane ("已配置命令") is a Vue ref the *parent* page binds
 * to — this component is intentionally dumb about visualization.  The
 * only state that lives here is what the terminal and the device need
 * to be happy; the textual command log is mirrored out via emits so the
 * parent can keep its tabs' logs in one place (so adding a new device
 * tab doesn't lose other tabs' logs when this component unmounts).
 *
 * Keystroke → right-pane sync algorithm:
 *   Every ``term.onData`` callback is a string of one or more keystrokes
 *   the operator just produced.  We push each character through a small
 *   state machine that maintains a per-session *line buffer*:
 *
 *     printable      → append
 *     \r or \n    → commit buffer (Enter pressed)
 *     \x7f / \x08  → pop one char (Backspace)
 *     \x1b          → keep buffer but drop & forward ESC verbatim
 *                       (the user is moving the cursor; we don't try
 *                        to interpret arrow / Home / End sequences)
 *     \x03 (Ctrl-C) → commit buffer (treat as send-and-cancel)
 *     \x04 (Ctrl-D) → commit buffer (EOF on current line)
 *     other control  → drop, do not log
 *
 *   We commit BEFORE forwarding so a rapid keystroke < line-shift > keystroke
 *   sequence still produces two right-pane rows in the right order.
 */
import { onActivated, onBeforeUnmount, onDeactivated, onMounted, nextTick, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Terminal } from 'xterm'
import { FitAddon } from 'xterm-addon-fit'
import 'xterm/css/xterm.css'
import { api } from '@/api'

export interface CommandEntry {
  text: string
  timestamp: string
}

const props = defineProps<{
  deviceId: number
  deviceName: string
  endpointId?: number | null
  credentialId?: number | null
  cols?: number
  rows?: number
  fontSize?: number
  maxLog?: number
  /**
   * Set by the parent when this session's tab is the one the user is
   * currently looking at.  We use it to:
   *   1. ``term.focus()`` on activation so keystrokes flow into the
   *      terminal without the operator having to click the canvas
   *      first (a common complaint when swapping tabs or returning
   *      from another page).
   *   2. trigger a ``fit()`` because the panel may have been hidden
   *      with ``display: none`` since the last render, leaving the
   *      xterm canvas at stale dimensions.
   */
  active?: boolean
  /**
   * When true, also paint printable bytes ourselves on top of
   * whatever the device echoes.  Set on a per-tab basis for
   * no-echo devices (a console server that strips echo, an EVE-NG
   * container that has no line discipline, Comware with
   * `undo terminal echo` etc.).
   *
   * Default: FALSE.  We trust the device to echo.  VRP / H3C / Cisco IOS
   * / Linux bash ALL echo on a real SSH session, so the default
   * produces doubled characters if local echo is also enabled.
   * Operators with a known no-echo device tick the per-tab toggle
   * to opt in to local echo.
   */
  echoLocal?: boolean
}>()

const emit = defineEmits<{
  (e: 'committed', entry: CommandEntry): void
  (e: 'draft', text: string): void
  (e: 'cleared'): void
  (e: 'status', payload: {
    deviceId: number
    status: 'idle' | 'connecting' | 'connected' | 'disconnected' | 'error'
    message?: string
  }): void
  /**
   * Emitted on every raw keystroke the operator types (before
   * forwarding to the device).  Used by the parent's broadcast mode:
   * when broadcast is on, the primary tab emits this event and the
   * parent re-dispatches the same data to every other selected tab
   * via ``sendAsIfTyped``.
   *
   * Distinct from ``draft`` (which reflects our local buffer state
   * post-edit) so the parent can mirror verbatim to siblings without
   * losing the original ESC/Ctrl sequences we intentionally swallow.
   */
  (e: 'keystroke', data: string): void
}>()

const containerRef = ref<HTMLDivElement | null>(null)
const status = ref<'idle' | 'connecting' | 'connected' | 'disconnected' | 'error'>('idle')
const message = ref('')
const connecting = ref(false)
const connected = ref(false)
// Disconnect-recovery state.  When the SSH channel dies (eNSP and
// similar virtual devices do this aggressively on idle / on the
// first error) the operator needs a way to bring the session back
// without reloading the page.  We expose a manual button rather
// than auto-reconnect so the operator stays in control of the
// teardown; see the template comment for the trade-off.
const reconnecting = ref(false)

let term: Terminal | null = null
let fit: FitAddon | null = null
let ws: WebSocket | null = null
let buffer = ''                // current uncommitted line
let resizeHandler: (() => void) | null = null
// ── Bug fix: lifecycle + focus guard ─────────────────────────────────────
//
// Two bugs are fixed here:
//
// Bug 1 — Focus race on tab open (fixed first in prior session):
//   All SshSession instances mount simultaneously (v-for renders all
//   tabs even when they are `display:none`).  The `immediate: true`
//   watcher fires before `mount()` runs, so `term` is null and
//   `focus()` is silently swallowed.  When the second tab's
//   `onMounted` fires, its `term.focus()` races against the first —
//   stealing keyboard focus from the tab the operator was using.
//
// Bug 2 — Blank canvas on return-from-nav (fixed now):
//   When the user navigates away from the SSH page, keep-alive keeps
//   the SshSession component alive, BUT onBeforeUnmount fires and
//   disposes the xterm instance.  When the user comes back,
//   onActivated runs, but `_ever_mounted` is STILL true (module-level
//   let is never reset), so `mount()` is skipped and `term` stays
//   null — the canvas is gone.
//
// Fix for both:
//   * `_is_mounted` tracks whether the xterm instance is currently
//     attached to the DOM (set true inside `mount()`, reset false in
//     onBeforeUnmount).  This is a *lifecycle* flag, not a one-way
//     "ever mounted" flag, so a future re-mount after eviction or
//     a keep-alive flush can re-create the terminal cleanly.
//   * `mount()` guards on `_is_mounted`: it only runs while the
//     component is actually mounted.  If Vue unmounts (keep-alive
//     edge case), the flag resets and the next onMounted can re-create.
//   * onBeforeUnmount closes the WebSocket (don't leak connections)
//     but intentionally does NOT dispose xterm — keep-alive keeps the
//     DOM subtree alive in memory and Vue will re-use it on
//     re-activation.  The xterm Terminal API writes directly to the
//     container div (not through Vue's vdom), so keeping the instance
//     across the route transition preserves the rendered buffer.
//   * onActivated re-fits + re-focuses only — never re-mounts.
let _is_mounted = false
// Track keep-alive deactivation: when parent MultiShellPage deactivates
// (route navigation away from /shell), this flag prevents onBeforeUnmount
// from closing WebSocket — the connection survives route transitions.
let _parent_deactivated = false
// Debounce map: only one offline-warning toast per device per 4s.
// Without this, holding down a key would spam 30 ElMessage boxes
// per second and lock the page in a flicker loop.
const lastOfflineWarnAt = new Map<number, number>()

// ── Echo suppression ring buffer ─────────────────────────────────────
//
// Problem: when `echoLocal=true` we paint printable bytes ourselves
// immediately, but the device ALSO echoes them back over the WebSocket.
// Without suppression the same character appears twice on screen.
//
// Solution: a small FIFO of printable bytes we just painted locally.
// When a device echo frame arrives we strip any leading bytes that
// match this buffer.  A 300ms auto-expiry handles the case where the
// device's echo never arrives (eNSP drops it, or the device really
// doesn't echo at all) — those bytes drain out so they don't bleed into
// later unrelated device output.
//
// Size 64 is plenty: xterm's max keystroke batch is the MTU (~1500 bytes)
// minus SSH framing overhead; practically a user pastes < 100 chars at once.
// Any overflow is silently dropped (it means something is wrong upstream
// and we prefer the device's echo to be visible rather than silent).
const SUPP_MAX = 64
const _suppBuf: string[] = []
let _suppTimer: ReturnType<typeof setTimeout> | null = null

function _pushSuppression(chars: string) {
  for (const ch of chars) {
    if (_suppBuf.length >= SUPP_MAX) _suppBuf.shift()
    _suppBuf.push(ch)
  }
  // Refresh expiry: if echo arrives within 300ms, suppress it; after
  // that the slot is cleared and future unrelated device output won't
  // accidentally consume a stale suppression byte.
  if (_suppTimer) clearTimeout(_suppTimer)
  _suppTimer = setTimeout(() => { _suppBuf.length = 0; _suppTimer = null }, 300)
}

// Strip up to `max` leading printable bytes from `buf` that match the
// suppression buffer.  Returns the trimmed payload and the consumed
// suppression bytes so the caller can advance _suppBuf accordingly.
function _stripSuppression(payload: string): string {
  let skip = 0
  for (const ch of payload) {
    const code = ch.charCodeAt(0)
    if (code < 0x20 && code !== 0x09) break        // control char — stop stripping
    if (code === 0x7f || code === 0x08) break      // backspace — stop stripping
    if (_suppBuf.length > 0 && _suppBuf[0] === ch) {
      _suppBuf.shift()
      skip++
    } else {
      break                                          // mismatch — stop stripping
    }
  }
  return skip > 0 ? payload.slice(skip) : payload
}

const MAX_LOG = props.maxLog ?? 500

function writeAnsi(s: string) {
  term?.write(s)
}

function banner() {
  if (!term) return
  writeAnsi('\x1b[90m──────────────────────────────────────────────\x1b[0m\r\n')
  writeAnsi(`\x1b[36m  NAW WebSSH\x1b[0m  ·  ${props.deviceName}\r\n`)
  writeAnsi('\x1b[90m──────────────────────────────────────────────\x1b[0m\r\n')
}

function mount() {
  if (!containerRef.value) return
  if (_is_mounted) return
  // xterm renders into whatever DOM container we hand it.  When the
  // tab is currently `display:none`, term.open() will silently
  // produce a 0×0 viewport, but the underlying canvas elements
  // exist in the DOM and become visible as soon as we flip display.
  // The next `fit()` (called right after open) clamps the geometry
  // to a sane default when the container is hidden, so the first
  // activation by `watch(active)` produces a clean refit rather
  // than the stale 0×0 the old code saw.
  _is_mounted = true
  term = new Terminal({
    cursorBlink: true,
    fontFamily: 'Menlo, Consolas, "Courier New", monospace',
    fontSize: props.fontSize ?? 14,
    cols: props.cols ?? 120,
    rows: props.rows ?? 30,
    convertEol: true,
    scrollback: 5000,
    theme: { background: '#0d1117', foreground: '#e6edf3' },
  })
  fit = new FitAddon()
  term.loadAddon(fit)
  term.open(containerRef.value)
  banner()

  term.onData((data) => onKey(data))

  resizeHandler = () => {
    try {
      fit?.fit()
    } catch {
      /* ignore */
    }
  }
  window.addEventListener('resize', resizeHandler)
  // Defer initial refits to the same robustFit used by activation.
  // We don't call fit() inline because the parent's display:none →
  // display:block transition may not have flushed layout yet.
  robustFit()
}

function doResize() {
  if (!term || !fit || !ws || ws.readyState !== WebSocket.OPEN) return
  try {
    fit.fit()
    ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
  } catch {
    /* ignore */
  }
}

// The DEBUG_SSH gate is the operator's way of turning trace
// logging on without recompiling.  Three activation modes are
// supported so a half-typed `localStorage` setter in the console
// still works:
//   1. `localStorage.setItem('DEBUG_SSH', '1')`  (the form a
//      console paste sometimes ends up as)
//   2. `localStorage.DEBUG_SSH = '1'`            (string truthy)
//   3. query parameter `?debug_ssh=1` (bookmarkable, no storage
//      touch — most reliable for a quick repro)
function debugSshOn(): boolean {
  if (typeof window === 'undefined') return false
  try {
    const q = new URLSearchParams(window.location.search)
    if (q.get('debug_ssh') === '1') return true
    if (window.localStorage?.getItem('DEBUG_SSH')) return true
  } catch {
    /* ignore */
  }
  return false
}

/**
 * Per-keystroke state machine.  See the file-level docstring for the
 * full rule table; the key invariant is:
 *   1. We mutate ``buffer`` *before* forwarding to the device, so the
 *      right pane reflects what the operator meant to type, not what
 *      the device eventually echoed (those can differ on slow links).
 *   2. We commit BEFORE forwarding, so simultaneous Enter + next-char
 *      sequences show the order the operator intended.
 */
function onKey(data: string) {
  if (!term) return
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    // Connection has been torn down.  The terminal is still rendering
    // (so the operator sees what they had) but we can't forward
    // keystrokes.  Surface a single warning per debounce window so
    // the operator knows to hit 重连 — silently swallowing every key
    // was the source of the "I type but nothing happens" complaint.
    if (debugSshOn()) {
      // eslint-disable-next-line no-console
      console.log(`[ssh:${props.deviceId}] DROP key (no ws/term); readyState=${ws?.readyState}`)
    }
    const now = Date.now()
    const last = lastOfflineWarnAt.get(props.deviceId) ?? 0
    if (now - last > 4000) {
      lastOfflineWarnAt.set(props.deviceId, now)
      ElMessage.warning(`${props.deviceName}：连接已断开，请点「重连」`)
    }
    return
  }
  if (debugSshOn()) {
    // eslint-disable-next-line no-console
    console.log(`[ssh:${props.deviceId}] TX`, JSON.stringify(data), 'echoLocal=', props.echoLocal, 'buffer=', JSON.stringify(buffer))
  }
  let next = buffer
  for (const ch of data) {
    const code = ch.charCodeAt(0)
    if (ch === '\r' || ch === '\n') {
      commit(next)
      next = ''
    } else if (code === 0x03 || code === 0x04) {
      commit(next)
      next = ''
    } else if (code === 0x7f || code === 0x08) {
      // Backspace: drop last char from local buffer AND emit the
      // canonical "\b \b" triple to xterm so the visible cursor
      // moves back, the cell is cleared, and the cursor advances
      // again.  Without this, a line-editing device (VRP, Cisco IOS
      // cooked mode, etc.) that does not echo the byte back leaves
      // the user staring at a half-typed prompt they cannot edit.
      // The device itself receives the raw \x7f and handles its own
      // line editing on its side.
      next = next.slice(0, -1)
    } else if (code === 0x1b) {
      // ESC + arrow keys. Don't append; the device side handles the
      // cursor move. We do not log "ESC was pressed" because every
      // ↑/↓ call would otherwise double up.
      next = ''
    } else if (code < 0x20 && code !== 0x09) {
      // Other control characters (Ctrl-A/E/K/W/...) are real editing
      // operations. We forward them, drop them from the buffer, and
      // leave any half-typed text intact for the user to re-evaluate.
    } else {
      next += ch
    }
  }
  buffer = next
  // Echo policy:
  //   * Default (`echoLocal=false`): trust the device to echo
  //     printable bytes.  We only paint the backspace "\b \b"
  //     locally because many VRP / H3C versions do not echo
  //     backspace erase sequences.
  //   * `echoLocal=true` (per-tab toggle): also paint printable
  //     bytes ourselves.  We feed the same printable bytes into
  //     the echo-suppression buffer so that when the device's echo
  //     arrives in ws.onmessage it is stripped before reaching
  //     term.write — preventing the double-paint the old code had.
  //     The suppression slot auto-expires after 300 ms so a missing
  //     echo (device that really doesn't echo) clears naturally.
  let localEchoBytes = ''
  for (const ch of data) {
    const code = ch.charCodeAt(0)
    if (code === 0x7f || code === 0x08) {
      term.write('\b \b')
    } else if (code >= 0x20 || code === 0x09) {
      if (props.echoLocal === true) {
        term.write(ch)
        localEchoBytes += ch
      }
    }
  }
  if (localEchoBytes) _pushSuppression(localEchoBytes)
  // Surface the raw keystroke *before* forwarding so the parent's
  // broadcast mode can re-dispatch to sibling tabs.  We do this even
  // in single-tab use — the event is cheap and the parent ignores it.
  emit('keystroke', data)
  try {
    const encoded = btoa(unescape(encodeURIComponent(data)))
    ws.send(JSON.stringify({ type: 'data', data: encoded }))
  } catch {
    /* ignore */
  }
  // Mirror the half-typed buffer to the parent so the right pane can
  // show it under "草稿" if it wants.
  emit('draft', buffer)
}

function commit(text: string) {
  const clean = text.trim()
  if (debugSshOn()) {
    // eslint-disable-next-line no-console
    console.log(`[ssh:${props.deviceId}] COMMIT text=${JSON.stringify(text)} clean=${JSON.stringify(clean)} early=${!clean ? 'YES' : 'no'}`)
  }
  if (!clean) return
  const entry: CommandEntry = {
    text: clean,
    timestamp: new Date().toISOString(),
  }
  emit('committed', entry)
}

function connect() {
  if (connecting.value || connected.value) return
  connecting.value = true
  status.value = 'connecting'
  emit('status', { deviceId: props.deviceId, status: 'connecting' })

  try {
    ws = api.openWebSsh(props.deviceId, {
      endpointId: props.endpointId ?? null,
      credentialId: props.credentialId ?? null,
    })
  } catch (e: unknown) {
    failConnect(e instanceof Error ? e.message : String(e))
    return
  }

  if (!ws) {
    failConnect('无法创建 WebSocket')
    return
  }
  ws.binaryType = 'arraybuffer'

  ws.onopen = () => {
    connected.value = true
    connecting.value = false
    status.value = 'connected'
    emit('status', { deviceId: props.deviceId, status: 'connected' })
    writeAnsi('\x1b[32m[NAW]\x1b[0m 已建立连接\r\n')
    doResize()
  }
  ws.onmessage = (ev) => {
    if (!term) return
    let msg: { type?: string; data?: string; message?: string }
    try {
      msg = JSON.parse(ev.data)
    } catch {
      return
    }
    if (msg.type === 'data' && typeof msg.data === 'string') {
      let payload = ''
      try {
        payload = atob(msg.data)
      } catch {
        /* ignore */
      }
      if (!payload) return
      // DEBUG: trace device→browser data so the operator can see in
      // the browser console whether the device is actually echoing
      // bytes back.  Activated by setting `localStorage.DEBUG_SSH=1`
      // and reloading.
      if (debugSshOn()) {
        // eslint-disable-next-line no-console
        console.log(`[ssh:${props.deviceId}] RX`, JSON.stringify(payload))
      }
      // Strip any local-echo bytes we painted in onKey.  The
      // suppression buffer is a short FIFO; bytes that match the
      // device's echo are consumed here and dropped before painting.
      // Any surplus in the buffer (device echo never arrived within
      // 300 ms) auto-expires naturally.
      payload = _stripSuppression(payload)
      if (payload) term.write(payload)
    } else if (msg.type === 'info') {
      const text = msg.message ?? ''
      writeAnsi(`\x1b[36m[NAW]\x1b[0m ${text}\r\n`)
      emit('status', { deviceId: props.deviceId, status: 'connected', message: text })
    } else if (msg.type === 'error') {
      const text = msg.message ?? '连接错误'
      writeAnsi(`\x1b[31m[NAW]\x1b[0m ${text}\r\n`)
      status.value = 'error'
      message.value = text
      emit('status', { deviceId: props.deviceId, status: 'error', message: text })
    }
  }
  ws.onclose = (ev) => {
    cleanupSocket()
    connected.value = false
    if (status.value !== 'error') {
      status.value = 'disconnected'
    }
    emit('status', {
      deviceId: props.deviceId,
      status: status.value === 'error' ? 'error' : 'disconnected',
      message: ev.code === 1000 ? '正常关闭' : `code=${ev.code}`,
    })
    const reason = ev.code === 1000 ? '正常关闭' : `code=${ev.code}`
    writeAnsi(`\x1b[33m[NAW]\x1b[0m 连接已断开（${reason}）\r\n`)
  }
  ws.onerror = () => {
    failConnect('WebSocket 错误（可能是身份过期或后端不可用）')
  }
}

function cleanupSocket() {
  if (ws) {
    try {
      ws.send(JSON.stringify({ type: 'close' }))
    } catch {
      /* ignore */
    }
    try {
      ws.close()
    } catch {
      /* ignore */
    }
    ws = null
  }
  _suppBuf.length = 0
  if (_suppTimer) { clearTimeout(_suppTimer); _suppTimer = null }
}

function disconnect() {
  cleanupSocket()
  connected.value = false
  status.value = 'disconnected'
  emit('status', { deviceId: props.deviceId, status: 'disconnected' })
}

/*
 * Banner-click handler.  We can't just bind `@click="connect"`
 * because the user might double-click the button before the new
 * socket is up, and a second connect() in flight would race
 * against the first.  The debounce is a small UX nicety — the
 * text also flips to '重连中…' so the user sees the click
 * registered even when nothing visible changes for a second.
 */
function onReconnectClick() {
  if (reconnecting.value) return
  reconnecting.value = true
  // Drop the old socket first so the next connect() doesn't
  // sit on a half-closed handle when the new handshake arrives.
  cleanupSocket()
  buffer = ''
  try {
    connect()
  } finally {
    setTimeout(() => {
      reconnecting.value = false
    }, 800)
  }
}


function failConnect(msg: string) {
  connecting.value = false
  status.value = 'error'
  message.value = msg
  ElMessage.error(`无法连接：${msg}`)
  emit('status', { deviceId: props.deviceId, status: 'error', message: msg })
}

defineExpose({
  connect,
  disconnect,
  doResize,
  clear: () => { buffer = ''; emit('cleared') },
  /**
   * Push a fixed string into this terminal exactly as if the operator
   * had typed it.  Used by the parent's broadcast mode: when one
   * tab is in "broadcast", every keystroke in the *primary* tab is
   * mirrored into the secondary tabs through this method, so all
   * sessions stay in lockstep.
   *
   * Important: we run the same state-machine as ``onKey`` so the
   * right-pane command log of the receiving tab also reflects what
   * was sent.  Without this, secondary tabs would receive the bytes
   * but their log panel would stay empty — defeating the whole point.
   */
  sendAsIfTyped: (text: string) => {
    if (!term || !ws || ws.readyState !== WebSocket.OPEN) return
    onKey(text)
  },
  /**
   * Focus the xterm canvas.  The parent calls this whenever this
   * session becomes the active tab (or when the route is restored
   * from keep-alive).  Without it, the user has to click the canvas
   * every time they switch tabs, which made "I can't type anything"
   * the most common complaint about the multi-shell view.
   */
  focusTerm: () => {
    try { term?.focus() } catch { /* ignore */ }
  },
  /**
   * Re-fit the terminal to its current container.  The parent
   * (MultiShellPage) calls this from its ``onActivated`` hook after
   * a route transition so every live session re-measures its
   * viewport at the new layout, even if the SshSession component
   * itself never had its onActivated fire (e.g. when the parent's
   * ``<keep-alive>`` re-activates the wrapper but a SshSession
   * child was never in the active slot).
   *
   * Safe to call before mount — it's a no-op until xterm is up.
   */
  refit: () => robustFit(),
})

/**
 * Lifecycle ordering (the actual sequence we follow):
 *
 *  1. Vue creates the SshSession vnode and inserts it into the
 *     parent's `terminal-pane` div.  Even tabs that are NOT the
 *     active one are kept in the DOM with `display:none` — this
 *     keeps the v-if transition cheap, but it also means EVERY
 *     SshSession is reachable via ref immediately.
 *
 *  2. `onMounted` runs.  We can't mount xterm here:
 *       a. The container is `display:none` on inactive tabs, so
 *          `term.open()` would create a 0×0 canvas that subsequent
 *          `fit()` calls cannot resize (xterm writes characters to
 *          fixed cell positions, so resizing the grid after the
 *          fact leaves a 0×0 viewport with hidden cells).
 *       b. Two SshSession instances created in the same tick would
 *          race their `term.focus()` calls and the second one
 *          steals focus from the tab the operator was typing in.
 *
 *  3. `watch(props.active)` fires whenever the parent flips the
 *     `active` prop (open new tab, switch tabs, come back from
 *     a different route via keep-alive).  On the FIRST
 *     `active=true` transition for this instance, we mount xterm.
 *     Subsequent active transitions are no-ops thanks to
 *     `_is_mounted`; we just call `fit()` + `focus()` to refit
 *     geometry and pull the cursor into the canvas.
 *
 *  4. `onActivated` handles keep-alive re-entry: the component was
 *     deactivated when the user navigated away, and the WS/xterm
 *     stayed alive in the cache.  When the user comes back, the
 *     canvas is already mounted — we just refit + refocus.
 */
onMounted(() => {
  // Intentionally a no-op: xterm is mounted in `watch(active)` below.
  // See the block comment for the why.
})

/**
 * Survive a parent route transition.  ``<keep-alive>`` swaps the
 * parent route's children in and out without unmounting them, so
 * the WebSocket and the xterm instance are preserved.  When we
 * come back, re-focus so the operator doesn't have to click before
 * typing.
 *
 * IMPORTANT: this fires for keep-alive re-activations (navigate-away
 * and back).  For those we MUST NOT re-mount xterm — the canvas is
 * already there, just hidden.  Re-fitting and re-focusing is enough.
 */
onActivated(() => {
  nextTick(() => {
    // Same multi-shot refit — coming back from a different route
    // means the layout has been recomputed and we want to land on
    // the final size, not whatever Chrome flushed at the first
    // paint frame.
    robustFit()
    if (props.active) {
      try { term?.focus() } catch { /* ignore */ }
    }
  })
})

/**
 * Mount the xterm when this tab becomes the active one.  The
 * `display:none → display:block` transition happens in the parent
 * template; once it settles, the container is visible and `mount()`
 * can safely call `fit()` without a stale 0×0 dimension.
 *
 * Critical: this watcher MUST use ``{ immediate: true }``.
 *
 * Why: the parent appends the new tab to ``sessions.value`` and
 * sets ``activeUid`` in the SAME tick.  So when this SshSession
 * first mounts, ``props.active`` is already ``true``.  A
 * non-immediate watch only fires on value CHANGES, so a
 * ``true → true`` would never trigger — the canvas would stay
 * blank (this is the exact bug the user reported as "终端整个
 * 空白,连 banner 都没有").
 *
 * We still need the ``containerRef`` to be set and the parent
 * to have applied ``display:block`` before we mount.  Both
 * happen on the same tick as the watch fire, so we defer to
 * ``nextTick`` (which the Vue scheduler runs after the DOM has
 * been written).  By the time the deferred callback runs,
 * containerRef is populated and the :style transition has
 * settled — exactly the conditions ``term.open()`` needs.
 *
 * Because `_is_mounted` guards the body, calling this on already-
 * mounted tabs is a no-op (safe to call repeatedly).
 */
watch(() => props.active, (isActive) => {
  if (!isActive) return
  // Defer so the DOM transition (display:none → block) has settled.
  nextTick(() => {
    mount()
    // Robust multi-shot refit.  The first fit after a fresh
    // term.open() can land before the parent's :style transition
    // has actually been applied (Chrome optimizes display flips
    // and sometimes defers layout).  60ms / 200ms / 500ms covers
    // the common cases — by 500ms the layout is stable for sure.
    robustFit()
    try { term?.focus() } catch { /* ignore */ }
  })
}, { immediate: true })

/**
 * Robust refit.  The first refit on a freshly-mounted terminal can
 * fail (or produce wrong dimensions) because:
 *
 *   * the parent template may still be in the middle of its
 *     `display:none → display:block` transition,
 *   * the .terminal-host div may not have been laid out yet
 *     (CSS grid/flexbox is async in some engines),
 *   * the keep-alive cache may hand us a container with stale
 *     dimensions from the previous route.
 *
 * We retry on three timeouts to cover all of these.  Each
 * subsequent attempt has a higher chance of finding the container
 * in its final size.  The fits are idempotent (FitAddon reads
 * the container's current size, not the terminal's), so calling
 * them multiple times in a row is cheap.
 */
function robustFit() {
  if (!fit) return
  const tries = [0, 60, 200, 500]
  for (const ms of tries) {
    setTimeout(() => {
      try { fit?.fit() } catch { /* ignore */ }
    }, ms)
  }
}

/**
 * Keep-alive lifecycle hooks.
 *
 * onDeactivated: set the deactivation guard flag so onBeforeUnmount
 * knows not to teardown the WebSocket when the parent route is
 * merely hidden. Also clear the echo-suppression timer (stale after
 * a long backgrounded period).
 *
 * onActivated: clear the guard flag and re-fit the terminal to
 * accommodate any layout changes that happened while we were cached.
 *
 * NOTE: Vue fires all registered callbacks in order. The ``watch``
 * with ``immediate: true`` at the bottom of this file also fires
 * on first activation; it handles the mount-vs-refit dispatch.
 * These two keep-alive hooks run after the watch callback.
 */
onDeactivated(() => {
  _parent_deactivated = true
  if (_suppTimer) { clearTimeout(_suppTimer); _suppTimer = null }
})

onActivated(() => {
  _parent_deactivated = false
  robustFit()
})

/**
 * Component teardown. During keep-alive deactivation (route navigation),
 * this hook STILL fires on v-for children even though parent is cached.
 * Check _parent_deactivated flag: if true, skip cleanup — the WebSocket
 * and terminal survive route transitions. Only cleanup on true destruction
 * (browser close, keep-alive cache eviction).
 */
onBeforeUnmount(() => {
  if (resizeHandler) window.removeEventListener('resize', resizeHandler)

  // Skip cleanup if parent is merely deactivated (keep-alive route navigation)
  if (!_parent_deactivated) {
    cleanupSocket()
    term?.dispose()
    term = null
    fit = null
  }

  _is_mounted = false
})

watch(() => props.cols, () => doResize())
watch(() => props.rows, () => doResize())
</script>

<template>
  <div class="ssh-session">
    <div class="terminal-host" ref="containerRef" />
    <div v-if="status === 'error' && message" class="err-tip">
      {{ message }}
    </div>
    <!--
      Disconnect banner.  Some backends (notably eNSP/VRP virtual
      devices) close the SSH channel after ~30 s of operator idle
      or after the first error response, leaving the operator with
      a live-looking terminal that no longer reaches the device.
      Expose a manual reconnect rather than an automatic one so
      that the operator is in control: they may have unsent input
      they want to review before the channel comes back, and
      auto-reconnect during an interactive paste / sequence can
      produce visible jumps.  The button just tears down the stale
      socket and opens a fresh one with the same credentials.
    -->
    <div v-if="status === 'disconnected'" class="disconnect-banner">
      <span class="disconnect-text">
        连接已断开（设备可能已主动关闭 SSH 通道，例如 eNSP 在闲置后断开）
      </span>
      <button class="disconnect-btn" :disabled="reconnecting" @click="onReconnectClick">
        {{ reconnecting ? '重连中…' : '重新连接' }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.ssh-session {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: #0d1117;
  position: relative;
}
.terminal-host {
  flex: 1;
  min-height: 0;
  background: #0d1117;
}
.err-tip {
  position: absolute;
  bottom: 12px;
  left: 12px;
  right: 12px;
  padding: 8px 12px;
  background: rgba(245, 108, 108, 0.15);
  border: 1px solid #f56c6c;
  border-radius: 4px;
  color: #f56c6c;
  font-size: 12px;
  font-family: Menlo, Consolas, monospace;
  pointer-events: none;
}
/*
 * Disconnect banner sits at the top so it doesn't cover the last
 * command the operator was typing.  Solid, opaque background so
 * it never gets confused with terminal output; the text is
 * deliberately short because the operator usually already knows
 * the channel is gone (their Enter just did nothing).
 */
.disconnect-banner {
  position: absolute;
  top: 12px;
  left: 12px;
  right: 12px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  background: rgba(240, 173, 78, 0.95);
  border: 1px solid #e6a23c;
  border-radius: 4px;
  color: #1a1a1a;
  font-size: 12px;
  font-family: Menlo, Consolas, monospace;
  z-index: 10;
}
.disconnect-text {
  flex: 1;
  min-width: 0;
}
.disconnect-btn {
  flex-shrink: 0;
  padding: 4px 12px;
  background: #1a1a1a;
  color: #f0ad4e;
  border: 1px solid #1a1a1a;
  border-radius: 3px;
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
}
.disconnect-btn:hover:not(:disabled) {
  background: #2c2c2c;
}
.disconnect-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
