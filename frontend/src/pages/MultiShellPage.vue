<script setup lang="ts">
// Keep-alive in App.vue caches this component across route changes so
// long-running WebSSH sessions survive a navigation.  The router-view
// key on keep-alive is the component's ``name`` option, and
// ``<script setup>`` doesn't expose one implicitly — so we declare
// it explicitly here.
defineOptions({ name: 'MultiShellPage' })
/**
 * Multi-device WebSSH aggregator with synchronized command panel.
 *
 * Each tab holds:
 *   ┌─────────────────────────────┬────────────────────────────┐
 *   │  xterm (real SSH session)   │  已配置命令 (sync buffer)  │
 *   └─────────────────────────────┴────────────────────────────┘
 *
 * Sync rule: every committed keystroke (Enter) goes into the right
 * pane.  Backspace edits the half-typed draft *before* it reaches the
 * device; if the device hasn't echoed yet the right pane still shows
 * the operator's intent.  Mirrored deletions come from the parent —
 * the parent owns the truth of the committed history; deleting an
 * entry in the right pane tells this tab to re-run ``clear`` + send
 * a synthetic "erase what I typed so far" sequence to the device so
 * the left visual matches.
 *
 * Per-device draft ("half-typed") line: the SshSession component emits
 * it on every keystroke; we keep it in a ``drafts`` map and show it as
 * a faded last line in the right pane so the operator sees what they're
 * about to commit.
 */
import { computed, nextTick, onActivated, onBeforeUnmount, onDeactivated, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  CircleClose,
  Connection,
  Monitor,
  Promotion,
  Refresh,
  Search,
  SwitchButton,
} from '@element-plus/icons-vue'
import SshSession from '@/components/SshSession.vue'
import type { CommandEntry } from '@/components/SshSession.vue'
import { api } from '@/api'

interface SessionConfig {
  /** Used internally so v-for stays stable across reconnects. */
  uid: string
  deviceId: number
  deviceName: string
  endpointId: number | null
  credentialId: number | null
  /** Last ``cols`` the operator picked. Persists across reconnects. */
  cols: number
  rows: number
  fontSize: number
  /**
   * Operator opt-in to local terminal echo for this tab.  The
   * default is false because most VRP / H3C / Cisco IOS / Linux
   * devices already echo on their own, and a blanket local echo
   * would double every key.  Toggle on for no-echo devices
   * (a console server that strips echo, EVE-NG, Comware with
   * echo off in user-interface vty).  Stored per-tab so the
   * operator doesn't have to remember to flip it for every new
   * session of the same kind.
   */
  echoLocal: boolean
}

interface DeviceRecord {
  id: number
  name: string
  // Server returns ``primary_endpoint`` (full DeviceEndpointOut or
  // null) — the old ``primary_endpoint_id`` was a hallucinated field
  // name that was never populated, so ``openDevice`` always started
  // sessions with ``endpointId = null``.  That worked only by accident
  // (the WebSSH handler falls back to the device's primary endpoint
  // when ``endpoint_id`` is None) but it lost the credential binding
  // and broke any code that read the field directly.
  primary_endpoint?: {
    id: number
    kind: string
    address: string
    port: number
    credential_id?: number | null
  } | null
  is_simulated?: boolean
}

// Full device payload returned by GET /devices/{id}; used by the
// in-picker edit dialog so it can pre-fill every editable field.
// Kept in lock-step with backend's DeviceOut / DeviceUpdateIn: vendor
// and endpoints are deliberately NOT editable from here.
interface DeviceEditForm {
  name: string
  hostname: string
  model: string
  software_version: string
  role: string
  serial_no: string
  location: string
  tags: string
  group_name: string
  notes: string
  vendor: string  // read-only display; backend PATCH rejects vendor changes
}

// ── session registry ────────────────────────────────────────────────────
const MAX_TABS = 6
const sessions = ref<SessionConfig[]>([])
const activeUid = ref<string>('')
const sessionRefs = ref<Record<string, InstanceType<typeof SshSession>>>({})

// Per-tab committed log + per-tab live draft.
const logs = ref<Record<string, CommandEntry[]>>({})
const drafts = ref<Record<string, string>>({})

// Tab metadata: status, message — each tab's SshSession emits these.
const statusMap = ref<Record<string, 'idle' | 'connecting' | 'connected' | 'disconnected' | 'error'>>({})
const messageMap = ref<Record<string, string>>({})

// ── device picker ──────────────────────────────────────────────────────
const allDevices = ref<DeviceRecord[]>([])
const deviceEndpoints = ref<Record<number, Array<Record<string, unknown>>>>({})
const allCredentials = ref<Array<Record<string, unknown>>>([])
const deviceSearch = ref('')
const pickerOpen = ref(false)
const pickerTab = ref<'existing' | 'quick'>('existing')
const pickerDeviceId = ref<number | null>(null)
const pickerEndpointId = ref<number | null>(null)
const pickerCredentialId = ref<number | null>(null)
const router = useRouter()
const route = useRoute()

// ── in-picker device edit dialog ────────────────────────────────
// Lets the operator fix a device's editable fields (name / hostname /
// location / group / notes / etc.) without leaving the multi-shell
// flow. Vendor and endpoints are intentionally NOT editable here —
// vendor can't be changed, and endpoints have their own management UI.
const editDialogOpen = ref(false)
const editBusy = ref(false)
const editError = ref('')
const editTargetId = ref<number | null>(null)
const editForm = reactive<DeviceEditForm>({
  name: '',
  hostname: '',
  model: '',
  software_version: '',
  role: 'switch',
  serial_no: '',
  location: '',
  tags: '',
  group_name: '',
  notes: '',
  vendor: '',
})

// Vendors in the same shape DevicesPage uses, so the edit dialog
// can render a greyed-out chip without re-deriving the display label.
const editVendorLabel = computed(() => {
  const map: Record<string, string> = {
    huawei_vrp: '华为 VRP',
    cisco_ios_xe: '思科 IOS-XE',
    ruijie_rgos: '锐捷 RGOS',
    simulated: '模拟设备',
  }
  // Explicit parens so babel's strict `?? ||` mixing rule is
  // satisfied regardless of which TS preset the bundler is
  // configured with (the previous short form
  // ``map[v] ?? v || '—'`` tripped the parser on cached Vite
  // workers and produced a blank page — see frontend_now.err).
  const fromMap = map[editForm.vendor]
  return fromMap ?? (editForm.vendor || '—')
})

// ── quick-connect (CRT-style: just type IP/port/creds) ──────────────
// Bypasses the device registry: the user types where to go, we POST
// a transient device+endpoint+credential record and route into the
// single-device CRT page. The transient device is marked so the user
// can prune it later.
const quickForm = reactive({
  address: '',
  port: 22,
  vendor: 'cisco_ios_xe',
  username: '',
  password: '',
  sshKey: '',
  kind: 'mgmt_ssh' as 'mgmt_ssh' | 'inband_ssh' | 'console',
  name: '',
  notes: '',
})
const quickBusy = ref(false)
const quickError = ref('')

// ── display opts (shared across all tabs) ─────────────────────────────
const defaults = ref({ cols: 120, rows: 32, fontSize: 14 })
const draftFilter = ref('')
// Default the options panel to open so the controls are discoverable;
// once the operator collapses it the value sticks within the
// session (not persisted across reloads — that's fine, the panel is
// small and reopening is one click).
const optionsOpen = ref<string[]>(['opts'])

// ── broadcast mode ───────────────────────────────────────────────────
// When broadcastMode is true, every keystroke in the *primary* tab is
// mirrored to all selected tabs in ``broadcastTargets``.  This is the
// "real ops" workflow: configure a switch's template once, fire it
// against SW01 / SW02 / SW03 in parallel — each device keeps its own
// terminal pane and command log so failures are easy to spot.
const broadcastMode = ref(false)
const broadcastTargets = ref<Set<string>>(new Set())

const activeSession = computed(() => sessions.value.find((s) => s.uid === activeUid.value) ?? null)
const activeLog = computed<CommandEntry[]>(() => {
  const list = logs.value[activeUid.value] ?? []
  if (!draftFilter.value.trim()) return list
  const q = draftFilter.value.toLowerCase()
  return list.filter((e) => e.text.toLowerCase().includes(q))
})
const activeDraft = computed(() => drafts.value[activeUid.value] ?? '')
const activeStatus = computed(() => statusMap.value[activeUid.value] ?? 'idle')
const activeMessage = computed(() => messageMap.value[activeUid.value] ?? '')

const visibleToc = computed(() => activeLog.value.slice(0, 200))

async function refreshPool() {
  try {
    const devs = await api.listDevices({ limit: 200 })
    // Backend returns a flat array; guard against paginated shape.
    allDevices.value = Array.isArray((devs as { items?: DeviceRecord[] }).items)
      ? (devs as { items: DeviceRecord[] }).items
      : (devs as DeviceRecord[])
    const creds = await api.listCredentials() as Array<Record<string, unknown>>
    allCredentials.value = creds
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail ?? '加载设备列表失败')
  }
}

async function loadEndpointsFor(deviceId: number) {
  if (deviceEndpoints.value[deviceId]) return
  try {
    const eps = await api.listDeviceEndpoints(deviceId) as Array<Record<string, unknown>>
    deviceEndpoints.value[deviceId] = eps
  } catch {
    deviceEndpoints.value[deviceId] = []
  }
}

function devicesNotOpened(): DeviceRecord[] {
  const openIds = new Set(sessions.value.map((s) => s.deviceId))
  const q = deviceSearch.value.trim().toLowerCase()
  return allDevices.value.filter((d) => !openIds.has(d.id) && (
    !q || d.name.toLowerCase().includes(q)
  ))
}

function openPicker() {
  pickerOpen.value = true
  if (!allDevices.value.length) {
    refreshPool()
  }
}

function openDevice(d: DeviceRecord) {
  if (sessions.value.some((s) => s.deviceId === d.id)) {
    const found = sessions.value.find((s) => s.deviceId === d.id)!
    activeUid.value = found.uid
    return
  }
  if (sessions.value.length >= MAX_TABS) {
    ElMessage.warning(`最多同时打开 ${MAX_TABS} 个终端，请先关闭一个`)
    return
  }
  const uid = `${d.id}-${Math.random().toString(36).slice(2, 8)}`
  const cfg: SessionConfig = {
    uid,
    deviceId: d.id,
    deviceName: d.name,
    endpointId: d.primary_endpoint?.id ?? null,
    credentialId: d.primary_endpoint?.credential_id ?? null,
    cols: defaults.value.cols,
    rows: defaults.value.rows,
    fontSize: defaults.value.fontSize,
    // Default ON — see the SshSession echoLocal docstring.  Most
    // operator-facing devices (VRP / H3C / IOS / Linux) echo on
    // their own and the operator can flip this off per tab if
    // they prefer the device's echo.  Defaulting on means
    // no-echo devices (console server / EVE-NG) are usable
    // out of the box.
    echoLocal: true,
  }
  sessions.value = [...sessions.value, cfg]
  logs.value[uid] = []
  drafts.value[uid] = ''
  statusMap.value[uid] = 'idle'
  messageMap.value[uid] = ''
  activeUid.value = uid
  pickerOpen.value = false
  // Auto-connect after the component is mounted.
  nextTick(() => {
    sessionRefs.value[uid]?.connect()
  })
}

function closeTab(uid: string) {
  sessionRefs.value[uid]?.disconnect()
  delete sessionRefs.value[uid]
  sessions.value = sessions.value.filter((s) => s.uid !== uid)
  delete logs.value[uid]
  delete drafts.value[uid]
  delete statusMap.value[uid]
  delete messageMap.value[uid]
  if (activeUid.value === uid) {
    activeUid.value = sessions.value[0]?.uid ?? ''
  }
}

async function closeTabWithConfirm(uid: string) {
  const st = statusMap.value[uid]
  if (st === 'connected') {
    try {
      await ElMessageBox.confirm('当前会话仍在运行，确定断开吗？', '关闭确认', {
        type: 'warning',
        confirmButtonText: '断开',
        cancelButtonText: '取消',
      })
    } catch {
      return
    }
  }
  closeTab(uid)
}

// ── device picker auto-pick endpoints/credentials ────────────────────
async function chooseDevice(id: number) {
  pickerDeviceId.value = id
  await loadEndpointsFor(id)
  const eps = deviceEndpoints.value[id] ?? []
  const primary = eps.find((e) => e.is_primary) ?? eps[0]
  pickerEndpointId.value = primary ? Number(primary.id) : null
  const dev = allDevices.value.find((d) => d.id === id)
  if (primary && primary.credential_id) {
    pickerCredentialId.value = Number(primary.credential_id)
  } else if (dev && dev.is_simulated) {
    pickerCredentialId.value = null
  } else {
    pickerCredentialId.value = null
  }
}

function submitPicker() {
  if (!pickerDeviceId.value) {
    ElMessage.warning('请选择设备')
    return
  }
  const d = allDevices.value.find((x) => x.id === pickerDeviceId.value)
  if (!d) return
  // Open the device, then patch the picked endpoint/credential into the
  // tab via the sessions ref.
  openDevice(d)
  const tab = sessions.value.find((s) => s.deviceId === d.id)
  if (tab) {
    tab.endpointId = pickerEndpointId.value ?? tab.endpointId
    tab.credentialId = pickerCredentialId.value
    // Force the active session to pick the new values up next connect.
    nextTick(() => sessionRefs.value[tab.uid]?.connect())
  }
}

// ── in-picker: open the edit dialog pre-filled with the device's
// current values. We GET the full device record (not just the list
// row) so every editable field is present. Vendor is captured for
// display only — it's greyed out because backend PATCH rejects it.
async function onEditDevice(id: number, evt?: Event) {
  evt?.stopPropagation()
  editError.value = ''
  try {
    const dev = await api.getDevice(id) as Partial<DeviceEditForm> & { id: number }
    editTargetId.value = id
    editForm.name = dev.name ?? ''
    editForm.hostname = dev.hostname ?? ''
    editForm.model = dev.model ?? ''
    editForm.software_version = dev.software_version ?? ''
    editForm.role = dev.role ?? 'switch'
    editForm.serial_no = dev.serial_no ?? ''
    editForm.location = dev.location ?? ''
    editForm.tags = dev.tags ?? ''
    editForm.group_name = dev.group_name ?? ''
    editForm.notes = dev.notes ?? ''
    editForm.vendor = dev.vendor ?? ''
    editDialogOpen.value = true
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail ?? '加载设备详情失败')
  }
}

async function submitEdit() {
  if (editTargetId.value == null) return
  if (!editForm.name.trim()) {
    editError.value = '名称不能为空'
    return
  }
  editBusy.value = true
  editError.value = ''
  try {
    // Only send fields the user can actually edit; backend will filter
    // again through DEVICE_PATCHABLE_FIELDS so this is just hygiene.
    const payload: Record<string, unknown> = {
      name: editForm.name.trim(),
      hostname: editForm.hostname.trim() || null,
      model: editForm.model.trim() || null,
      software_version: editForm.software_version.trim() || null,
      role: editForm.role.trim() || 'switch',
      serial_no: editForm.serial_no.trim() || null,
      location: editForm.location.trim() || null,
      tags: editForm.tags.trim() || null,
      group_name: editForm.group_name.trim() || null,
      notes: editForm.notes.trim() || null,
    }
    await api.updateDevice(editTargetId.value, payload)
    ElMessage.success('设备已更新')
    editDialogOpen.value = false
    editTargetId.value = null
    // Refresh the device pool so the picker list and the cached
    // allDevices reflect the new name/values.
    await refreshPool()
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    editError.value = detail ?? '更新失败'
  } finally {
    editBusy.value = false
  }
}

function cancelEdit() {
  if (editBusy.value) return
  editDialogOpen.value = false
  editTargetId.value = null
  editError.value = ''
}

// In-picker delete: requires admin (backend enforces via require_admin).
// We always double-confirm because this cascades through endpoints /
// jobs / drift data — easy to recover from a fresh import, but
// surprising if the operator just wanted to "fix a typo".
async function onDeleteDevice(id: number, name: string, evt?: Event) {
  evt?.stopPropagation()
  try {
    await ElMessageBox.confirm(
      `确认删除设备「${name}」？此操作不可撤销，会一并清理其连接点 / 关联任务 / 漂移历史。`,
      '删除设备',
      {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        // The native ``el-button--danger`` puts white text on a red
        // background, which reads as low-contrast / disabled.  Use a
        // dedicated class so we can paint a red border + red text on
        // a white surface (the convention for "destructive" buttons
        // in most modern apps) and the dialog still feels destructive
        // without looking washed out.
        confirmButtonClass: 'naw-danger-text',
      },
    )
  } catch {
    return  // user cancelled
  }
  try {
    await api.deleteDevice(id)
    ElMessage.success(`设备「${name}」已删除`)
    // If the user had staged this device in the picker, clear the
    // pending selection so submitPicker doesn't fire against a ghost.
    if (pickerDeviceId.value === id) {
      pickerDeviceId.value = null
      pickerEndpointId.value = null
      pickerCredentialId.value = null
    }
    // Drop any endpoints we cached for this device.
    delete deviceEndpoints.value[id]
    // Refresh pool so the deleted row vanishes.
    await refreshPool()
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    ElMessage.error(detail ?? '删除失败')
  }
}

// Quick-connect: create a transient device+endpoint+credential, then
// route into the single-device CRT page. Mirrors the CRT "type IP
// + username/password and go" workflow without forcing the user to
// pre-register the box.
async function submitQuickConnect() {
  quickError.value = ''
  const addr = quickForm.address.trim()
  if (!addr) { quickError.value = '请填写 IP / 主机名'; return }
  if (!quickForm.username.trim()) { quickError.value = '请填写用户名'; return }
  if (!quickForm.password && !quickForm.sshKey) {
    quickError.value = '密码或 SSH 私钥二选一'
    return
  }
  const port = Number(quickForm.port) || 22
  const vendor = quickForm.vendor
  const kind = quickForm.kind
  // Vendor 'simulated' is server-derived, so we don't expose it here.
  // The user picks a real vendor and types the target.
  quickBusy.value = true
  try {
    // 1. create credential first
    const cred = await api.createCredential({
      name: `quick-${addr}-${port}-${Date.now()}`,
      username: quickForm.username,
      password: quickForm.password || null,
      ssh_key: quickForm.sshKey || null,
      secret_kind: quickForm.sshKey ? 'ssh_key' : 'password',
    }) as { id: number }
    // 2. create device with embedded primary endpoint
    const deviceName = quickForm.name.trim() || `tmp-${addr}-${port}`
    const dev = await api.createDevice({
      name: deviceName,
      hostname: addr,
      vendor,
      role: kind === 'console' ? 'console' : 'switch',
      notes: '通过快速连接创建的临时设备',
      endpoints: [{
        kind,
        address: addr,
        port,
        credential_id: cred.id,
        is_primary: true,
        notes: 'quick-connect',
      }],
    }) as { id: number }
    pickerOpen.value = false
    ElMessage.success(`已创建临时设备 #${dev.id}，正在打开…`)
    router.push({ path: `/devices/${dev.id}/shell` })
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    quickError.value = detail ?? (e instanceof Error ? e.message : '创建失败')
  } finally {
    quickBusy.value = false
  }
}

// ── session event sinks ──────────────────────────────────────────────
function onCommitted(uid: string, entry: CommandEntry) {
  const arr = logs.value[uid] ?? []
  // Newest first so a brand-new entry is at the top.
  logs.value[uid] = [entry, ...arr].slice(0, 500)
}
function onDraft(uid: string, text: string) {
  drafts.value[uid] = text
}
function onStatus(uid: string, payload: {
  deviceId: number
  status: 'idle' | 'connecting' | 'connected' | 'disconnected' | 'error'
  message?: string
}) {
  statusMap.value[uid] = payload.status
  if (payload.message) messageMap.value[uid] = payload.message
}

/**
 * Broadcast dispatcher.  When the operator is in the primary tab and
 * broadcast mode is on, every keystroke they produce is mirrored into
 * every other selected tab via ``sendAsIfTyped``.  We do *not* echo
 * the keystroke back into the primary tab — its own ``onKey`` already
 * routed it through the local state machine and forwarded it to the
 * device.  We also deliberately skip tabs that aren't connected —
 * SSH is a wire protocol; sending into a closed socket would just
 * queue up text that arrives 30s later when the user reconnects.
 */
function onKeystroke(sourceUid: string, data: string) {
  if (!broadcastMode.value) return
  if (sourceUid !== activeUid.value) return
  for (const uid of broadcastTargets.value) {
    if (uid === sourceUid) continue
    if (statusMap.value[uid] !== 'connected') continue
    sessionRefs.value[uid]?.sendAsIfTyped(data)
  }
}

function toggleBroadcastTarget(uid: string) {
  const next = new Set(broadcastTargets.value)
  if (next.has(uid)) next.delete(uid)
  else next.add(uid)
  broadcastTargets.value = next
}

function selectAllBroadcast() {
  broadcastTargets.value = new Set(
    sessions.value
      .filter((s) => s.uid !== activeUid.value)
      .map((s) => s.uid),
  )
}
function clearBroadcast() { broadcastTargets.value = new Set() }

// ── Right pane is read-only ────────────────────────────────────────────
// Per spec: the right pane is a *passive mirror* of what the operator
// has typed into the device.  Operators cannot delete individual
// rows, replay them, or clear the buffer from here — if a line was
// wrong, the operator fixes it on the device side, and the right
// pane will catch up via the next event (a backspace removes chars
// from the in-flight draft; a fresh commit overwrites the wrong line
// in spirit, even if the wrong line is still visible as history).
//
// All three actions — ``deleteLogEntry``, ``recall``,
// ``clearLog`` — have therefore been removed.  The buttons and event
// bindings that called them are gone from the template below.

// ── tab UI helpers ──────────────────────────────────────────────────
function tabStatus(uid: string) {
  return statusMap.value[uid] ?? 'idle'
}
function tabStatusType(s: string) {
  switch (s) {
    case 'connected': return 'success'
    case 'connecting': return 'warning'
    case 'error': return 'danger'
    case 'disconnected': return 'info'
    default: return 'info'
  }
}
function tabStatusLabel(s: string) {
  switch (s) {
    case 'idle': return '未连接'
    case 'connecting': return '连接中'
    case 'connected': return '已连接'
    case 'disconnected': return '已断开'
    case 'error': return '错误'
    default: return s
  }
}

function reconnect(uid: string) {
  sessionRefs.value[uid]?.connect()
}

// ── active tab operation handoff ─────────────────────────────────────
function activeConnect() {
  if (!activeUid.value) return
  sessionRefs.value[activeUid.value]?.connect()
}
function activeDisconnect() {
  if (!activeUid.value) return
  sessionRefs.value[activeUid.value]?.disconnect()
}
function activeResize() {
  if (!activeUid.value) return
  sessionRefs.value[activeUid.value]?.doResize()
}

/**
 * Per-tab toggle for local terminal echo.  The SshSession reads
 * ``props.echoLocal`` on every keystroke (Vue keeps the prop
 * reactive), so flipping it takes effect on the very next
 * character — no reconnect required.
 *
 * We mutate the session in place (rather than rebuilding
 * ``sessions.value``) to keep the v-for stable.  A fresh array
 * would re-key every tab, which would tear down all sibling
 * WebSSH sessions.
 */
function toggleEchoLocal(value: boolean) {
  const tab = sessions.value.find((s) => s.uid === activeUid.value)
  if (!tab) return
  tab.echoLocal = value
}

/**
 * Tab click handler: switch the active session and pull focus into
 * the new xterm canvas so the operator can keep typing without
 * having to click the terminal first.  Without this, switching
 * tabs while another element (broadcast chip, the log filter) is
 * focused would leave the terminal "stuck" until the next click.
 */
function focusTab(uid: string) {
  if (activeUid.value === uid) {
    // Re-clicking the active tab still refocuses (useful after the
    // user clicked into the right-hand log filter and wants to
    // return to typing).
    sessionRefs.value[uid]?.focusTerm?.()
    return
  }
  activeUid.value = uid
  // Defer to nextTick so the :style flip and the resulting refit
  // happen before we ask xterm to focus the canvas.
  nextTick(() => sessionRefs.value[uid]?.focusTerm?.())
}

function updateDefaults(key: 'cols' | 'rows' | 'fontSize', v: number) {
  defaults.value[key] = v
  // Apply to all open tabs so the change is consistent.
  for (const s of sessions.value) s[key] = v
}

// ── lifecycle ─────────────────────────────────────────────────────
onBeforeUnmount(() => {
  for (const s of sessions.value) {
    sessionRefs.value[s.uid]?.disconnect()
  }
})

/**
 * keep-alive hooks.  The whole `<AppLayout>` is wrapped in
 * `<keep-alive>` in App.vue, so navigating to /dashboard (全网概况)
 * /ai (AI 助手) /configs (配置中心) /tasks (批量任务) deactivates
 * this page; coming back re-activates it.  We use these hooks to
 * re-fit every live terminal to the post-transition layout — the
 * SshSession children also have their own onActivated, but this
 * top-level hook is the safety net that catches every session
 * (including ones that never had props.active flip).
 */
onActivated(() => {
  // Defer so the AppLayout's flex/grid sizing has settled after
  // the route transition.  Without nextTick we'd often refit
  // against a half-rendered container and snap the grid back to
  // its previous (now stale) size.
  nextTick(() => {
    for (const ref of Object.values(sessionRefs.value)) {
      try { ref?.refit?.() } catch { /* ref may be stale — ignore */ }
    }
  })
})

onDeactivated(() => {
  // The sessionRefs' underlying SshSession children are kept alive
  // by the parent's keep-alive; their WebSockets stay open.  We
  // intentionally do nothing here — closing sockets on a deactivation
  // would defeat the point of keep-alive, and re-establishing them
  // on every activation is exactly the bug the user reported.
})

watch(pickerOpen, (open) => {
  if (open && !allDevices.value.length) refreshPool()
})
// When the picker settles on a device but endpoints haven't been
// fetched yet, kick off the fetch so the connection-point dropdown
// populates without the operator having to wait.
watch(pickerDeviceId, (id) => {
  if (id) loadEndpointsFor(id)
})

// Accept a deep link like /shell?devices=1,2,3 — caller wants to
// pre-open a list of devices.  We parse it lazily once devices load.
// Also re-runs whenever the route's `devices` query changes, so
// navigating from another page (e.g. quick-connect, device detail)
// opens the requested device as a NEW tab and switches to it.
function tryOpenFromQuery(raw: string | null | undefined) {
  if (!raw) return
  const ids = raw.split(',').map((x) => parseInt(x, 10)).filter((x) => !Number.isNaN(x))
  if (!ids.length) return
  // Defer until devices have loaded.
  const open = () => {
    for (const id of ids) {
      const d = allDevices.value.find((x) => x.id === id)
      if (d && !sessions.value.some((s) => s.deviceId === d.id)) {
        openDevice(d)  // sets activeUid + auto-connects
      } else if (d) {
        // Already an open session for this device — just focus it.
        const existing = sessions.value.find((s) => s.deviceId === d.id)
        if (existing) activeUid.value = existing.uid
      }
    }
  }
  if (allDevices.value.length) open()
  else {
    refreshPool().then(open)
  }
}

// React to query changes (e.g. quick-connect pushes
// /shell?devices=<newId>).  Reusing the same component instance
// means onMounted only runs once per page load — without this watch
// the second navigation silently no-ops.
watch(
  () => route.query.devices,
  (raw) => {
    if (typeof raw === 'string') tryOpenFromQuery(raw)
  },
)

// Initial mount: load the pool and check for any deep link in the URL.
if (typeof window !== 'undefined') {
  refreshPool()
  if (route.query.devices) {
    tryOpenFromQuery(String(route.query.devices))
  }
}
</script>

<template>
  <div class="page multi-shell-page">
    <!-- ── header ────────────────────────────────────────── -->
    <el-card class="layout-card" shadow="never">
      <template #header>
        <div class="header">
          <div class="title">
            <el-icon><Monitor /></el-icon>
            <span>WebSSH · 多设备终端</span>
            <el-tag size="small" effect="plain">{{ sessions.length }} / {{ MAX_TABS }}</el-tag>
          </div>
          <div class="header-actions">
            <el-button :icon="Refresh" plain @click="refreshPool">刷新设备</el-button>
            <el-button type="primary" :icon="Connection" @click="openPicker">
              添加设备
            </el-button>
          </div>
        </div>
      </template>

      <!-- ── tab strip ─────────────────────────────────────── -->
      <div v-if="sessions.length" class="tab-strip">
        <div
          v-for="s in sessions"
          :key="s.uid"
          class="tab"
          :class="{
            active: s.uid === activeUid,
            'tab-broadcast': broadcastTargets.has(s.uid),
          }"
          @click="focusTab(s.uid)"
        >
          <el-checkbox
            v-if="broadcastMode && s.uid !== activeUid"
            :model-value="broadcastTargets.has(s.uid)"
            class="tab-broadcast-check"
            @change="() => toggleBroadcastTarget(s.uid)"
            @click.stop
          />
          <span class="tab-name" :title="s.deviceName">
            <el-icon><Connection /></el-icon>
            <span class="tab-name-text">{{ s.deviceName }}</span>
          </span>
          <el-tag size="small" :type="tabStatusType(tabStatus(s.uid))" effect="dark">
            {{ tabStatusLabel(tabStatus(s.uid)) }}
          </el-tag>
          <el-button
            size="small"
            link
            type="danger"
            class="tab-close"
            :title="tabStatus(s.uid) === 'connected' ? '断开会话并关闭' : '关闭标签'"
            @click.stop="closeTabWithConfirm(s.uid)"
          >
            <el-icon><CircleClose /></el-icon>
          </el-button>
        </div>
      </div>

      <!-- ── main split: terminal | log ────────────────── -->
      <div v-if="activeSession" class="split">
        <!--
          Inactive tabs are kept mounted (xterm + WebSocket alive)
          but visually hidden.  With App.vue's <keep-alive> now
          working correctly (router-view + v-slot pattern), the
          SshSession child components survive route transitions
          as full instances — WebSockets stay connected, xterm
          canvas contents stay intact, and the user can tab back
          and forth without reconnection.

          Why ``display: none`` here is safe now:
            1. The component instance is preserved by keep-alive,
               so we don't unmount/remount on tab switch.
            2. The container is toggled by Vue's reactive style
               binding — the *layout transition* happens
               synchronously in the same tick, and xterm.js's
               ``fit()`` is called in the same ``watch(props.active)``
               callback, so the canvas gets re-measured
               immediately after ``display`` flips to ``block``.
            3. Chromium only drops the canvas backing store when
               ``display: none`` lasts long enough for the
               compositor to evict it — that doesn't happen on
               a user-driven tab click (sub-second), and the
               60/200/500ms ``robustFit`` ladder in
               SshSession.vue catches the rare case where it
               does.
        -->
        <div class="terminal-pane">
          <SshSession
            v-for="s in sessions"
            :key="s.uid"
            :ref="(el) => { if (el) sessionRefs[s.uid] = el as InstanceType<typeof SshSession> }"
            :device-id="s.deviceId"
            :device-name="s.deviceName"
            :endpoint-id="s.endpointId"
            :credential-id="s.credentialId"
            :cols="s.cols"
            :rows="s.rows"
            :font-size="s.fontSize"
            :active="s.uid === activeUid"
            :echo-local="s.echoLocal"
            :style="{ display: s.uid === activeUid ? 'block' : 'none' }"
            @committed="(e: CommandEntry) => onCommitted(s.uid, e)"
            @draft="(text: string) => onDraft(s.uid, text)"
            @cleared="drafts[s.uid] = ''"
            @status="(p) => onStatus(s.uid, p)"
            @keystroke="(data: string) => onKeystroke(s.uid, data)"
          />
        </div>

        <!-- right: synchronized command log -->
        <aside class="log-pane">
          <!--
            Top row: device name + status + the four connection ops.
            We deliberately put the connection state and the
            connect/disconnect/reconnect buttons in one row so the
            operator can see "am I connected?" and act on it without
            scanning the page.  Filter input, terminal-size sliders
            and broadcast controls all moved into the collapsible
            "options" panel below to keep this row uncluttered.
          -->
          <div class="log-header">
            <span class="log-title">
              <el-icon><Monitor /></el-icon>
              <span class="log-title-text">已配置命令</span>
              <el-tag size="small" effect="plain">{{ activeLog.length }}</el-tag>
              <el-tag
                v-if="activeStatus"
                size="small"
                :type="tabStatusType(activeStatus) as any"
                effect="dark"
              >
                {{ tabStatusLabel(activeStatus) }}
              </el-tag>
            </span>
            <el-button-group class="log-header-actions">
              <el-tooltip
                content="对当前标签启用本地回显：当设备不回显时（console server / EVE-NG 容器 / 关闭了 echo 的 Comware），打开此开关可直接看到自己输入的字符。设备本身如果回显，可能出现字符重复。"
                placement="top"
              >
                <el-checkbox
                  :model-value="activeSession?.echoLocal ?? false"
                  class="echo-local-toggle"
                  @change="(v: boolean | string | number) => toggleEchoLocal(!!v)"
                >
                  显示输入
                </el-checkbox>
              </el-tooltip>
              <el-button
                size="small"
                type="primary"
                plain
                :disabled="!activeSession || activeStatus === 'connected' || activeStatus === 'connecting'"
                @click="activeConnect"
                title="连接"
              >
                <el-icon><Promotion /></el-icon>
              </el-button>
              <el-button
                size="small"
                type="danger"
                plain
                :disabled="!activeSession || activeStatus !== 'connected'"
                @click="activeDisconnect"
                title="断开"
              >
                <el-icon><SwitchButton /></el-icon>
              </el-button>
              <el-button
                v-if="activeStatus === 'error' || activeStatus === 'disconnected'"
                size="small"
                plain
                @click="reconnect(activeUid)"
                title="重连"
              >
                <el-icon><Refresh /></el-icon>
              </el-button>
            </el-button-group>
          </div>

          <el-text v-if="activeMessage" type="danger" size="small" class="log-message">
            {{ activeMessage }}
          </el-text>

          <!--
            Collapsible options panel.  The terminal-size controls,
            broadcast toggle, and command filter are all "set once,
            ignore afterwards" knobs — keeping them visible 100% of
            the time wasted ~80px of vertical space and made the log
            pane feel claustrophobic on laptops.  Default to expanded
            so first-time users can find them; once collapsed they
            stay out of the way.
          -->
          <el-collapse v-model="optionsOpen" class="log-options">
            <el-collapse-item title="终端选项与筛选" name="opts">
              <div class="opt-row">
                <span class="opt-label">终端</span>
                <el-input-number
                  :model-value="defaults.cols"
                  :min="40"
                  :max="400"
                  size="small"
                  controls-position="right"
                  @change="(v: number) => updateDefaults('cols', v)"
                />
                <span class="opt-x">×</span>
                <el-input-number
                  :model-value="defaults.rows"
                  :min="10"
                  :max="120"
                  size="small"
                  controls-position="right"
                  @change="(v: number) => updateDefaults('rows', v)"
                />
                <el-button size="small" plain @click="activeResize">自适应</el-button>
              </div>
              <div class="opt-row">
                <span class="opt-label">字号</span>
                <el-slider
                  :model-value="defaults.fontSize"
                  :min="10"
                  :max="22"
                  :step="1"
                  style="flex: 1; min-width: 0"
                  @change="(v: number | number[]) => updateDefaults('fontSize', Array.isArray(v) ? v[0] : v)"
                />
                <span class="opt-mono">{{ defaults.fontSize }}px</span>
              </div>
              <div class="opt-row">
                <el-checkbox v-model="broadcastMode" :disabled="sessions.length < 2">
                  广播模式
                </el-checkbox>
                <el-tooltip
                  v-if="sessions.length >= 2"
                  content="开启后,在当前标签输入的所有内容会同步发送到所选的其他标签"
                  placement="top"
                >
                  <el-icon class="hint-icon"><Monitor /></el-icon>
                </el-tooltip>
                <template v-if="broadcastMode">
                  <el-tag size="small" type="success" effect="dark">
                    广播目标 {{ broadcastTargets.size }} 台
                  </el-tag>
                  <el-button size="small" link @click="selectAllBroadcast">全选其他</el-button>
                  <el-button size="small" link @click="clearBroadcast">清空</el-button>
                </template>
              </div>
              <div class="opt-row">
                <el-input
                  v-model="draftFilter"
                  size="small"
                  placeholder="筛选命令…"
                  clearable
                  style="flex: 1; min-width: 0"
                >
                  <template #prefix><el-icon><Search /></el-icon></template>
                </el-input>
              </div>
            </el-collapse-item>
          </el-collapse>

          <div class="log-list">
            <!-- Current draft (live) — half-typed, between operator and device -->
            <div v-if="activeDraft" class="log-row log-row-draft">
              <span class="log-time">{{ new Date().toLocaleTimeString() }}</span>
              <code class="log-text">{{ activeDraft }}</code>
              <el-tag size="small" type="info" effect="plain">待发送</el-tag>
            </div>
            <div v-if="!activeLog.length && !activeDraft" class="empty">
              暂无配置命令。在左侧终端输入命令后回车即可在此记录。
            </div>
            <div
              v-for="(entry, idx) in activeLog"
              :key="`${entry.timestamp}-${idx}`"
              class="log-row"
            >
              <span class="log-time">{{ new Date(entry.timestamp).toLocaleTimeString() }}</span>
              <!--
                Right pane is strictly read-only: no click handler, no
                delete button, no title hint suggesting interaction.
                Operators edit via the left terminal; this panel reflects
                the keystrokes, nothing more.
              -->
              <code class="log-text">{{ entry.text }}</code>
              <span class="log-marker" aria-hidden="true">·</span>
            </div>
          </div>
        </aside>
      </div>

      <!-- ── empty state ───────────────────────────────────── -->
      <el-empty
        v-else
        description="未添加任何终端设备，点击右上角「添加设备」开始"
        :image-size="80"
      />
    </el-card>

    <!-- ── device picker dialog ───────────────────────── -->
    <el-dialog v-model="pickerOpen" title="添加设备" width="640px" align-center>
      <el-tabs v-model="pickerTab" type="border-card">
        <el-tab-pane label="已有设备" name="existing">
          <el-form label-width="80px">
            <el-form-item label="设备">
              <el-input v-model="deviceSearch" placeholder="搜索设备名" clearable />
              <el-table
                :data="devicesNotOpened()"
                highlight-current-row
                @current-change="(row: DeviceRecord | null) => row && chooseDevice(row.id)"
                max-height="240"
                size="small"
                style="margin-top: 8px"
              >
                <el-table-column prop="id" label="#" width="60" />
                <el-table-column prop="name" label="名称" />
                <el-table-column label="主用连接点" width="180">
                  <template #default="{ row }">
                    <template v-if="row.primary_endpoint">
                      <code class="mono">{{ row.primary_endpoint.address }}:{{ row.primary_endpoint.port }}</code>
                      <el-tag size="small" type="info" effect="plain" style="margin-left:6px">
                        {{ row.primary_endpoint.kind }}
                      </el-tag>
                    </template>
                    <span v-else class="muted">—</span>
                  </template>
                </el-table-column>
                <el-table-column label="选择" width="80" align="center">
                  <template #default="{ row }">
                    <el-radio v-model="pickerDeviceId" :value="row.id">选</el-radio>
                  </template>
                </el-table-column>
                <el-table-column label="操作" width="150" align="center" fixed="right">
                  <template #default="{ row }">
                    <el-button
                      size="small"
                      link
                      type="primary"
                      @click.stop="onEditDevice(row.id, $event)"
                    >编辑</el-button>
                    <el-button
                      size="small"
                      link
                      type="danger"
                      @click.stop="onDeleteDevice(row.id, row.name, $event)"
                    >删除</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </el-form-item>
            <el-form-item label="连接点">
              <el-select v-model="pickerEndpointId" placeholder="选择连接点" style="width:100%">
                <el-option
                  v-for="ep in (deviceEndpoints[pickerDeviceId ?? -1] ?? [])"
                  :key="String(ep.id)"
                  :value="Number(ep.id)"
                  :label="`${ep.kind} · ${ep.address}:${ep.port}${ep.is_primary ? ' · 主用' : ''}`"
                />
              </el-select>
              <div v-if="!deviceEndpoints[pickerDeviceId ?? -1]?.length && pickerDeviceId" class="hint">
                该设备尚未配置任何连接点。请到「设备详情 → 连接配置」中添加。
              </div>
            </el-form-item>
            <el-form-item label="凭据">
              <el-select v-model="pickerCredentialId" placeholder="选择凭据" filterable clearable style="width:100%">
                <el-option
                  v-for="c in allCredentials"
                  :key="String(c.id)"
                  :value="Number(c.id)"
                  :label="`${c.name}（${c.username} / ${c.secret_kind}）`"
                />
              </el-select>
            </el-form-item>
          </el-form>
        </el-tab-pane>
        <el-tab-pane label="快速连接（CRT 风格）" name="quick">
          <el-form label-width="92px" size="small">
            <el-form-item label="名称（可选）">
              <el-input v-model="quickForm.name" placeholder="留空则自动生成 tmp-<ip>-<port>" clearable />
            </el-form-item>
            <el-form-item label="目标 IP / 主机" required>
              <el-input v-model="quickForm.address" placeholder="例如 10.0.0.1 或 core-sw-01.example.com" clearable />
            </el-form-item>
            <el-form-item label="协议">
              <el-select v-model="quickForm.kind" style="width:100%">
                <el-option label="管理 SSH（推荐）" value="mgmt_ssh" />
                <el-option label="业务 SSH" value="inband_ssh" />
                <el-option label="Console（串口）" value="console" />
              </el-select>
            </el-form-item>
            <el-form-item label="端口">
              <el-input-number v-model="quickForm.port" :min="1" :max="65535" :step="1" style="width: 100%" />
            </el-form-item>
            <el-form-item label="厂商">
              <el-select v-model="quickForm.vendor" style="width:100%">
                <el-option label="思科 IOS-XE" value="cisco_ios_xe" />
                <el-option label="华为 VRP" value="huawei_vrp" />
                <el-option label="锐捷 RGOS" value="ruijie_rgos" />
              </el-select>
            </el-form-item>
            <el-form-item label="用户名" required>
              <el-input v-model="quickForm.username" placeholder="SSH 用户名" clearable />
            </el-form-item>
            <el-form-item label="密码">
              <el-input v-model="quickForm.password" type="password" show-password placeholder="密码（与 SSH 私钥二选一）" clearable />
            </el-form-item>
            <el-form-item label="SSH 私钥">
              <el-input v-model="quickForm.sshKey" type="textarea" :rows="3" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----（二选一时填这里）" />
            </el-form-item>
            <el-form-item v-if="quickError" label=" ">
              <el-text type="danger" size="small">{{ quickError }}</el-text>
            </el-form-item>
            <el-form-item label="备注">
              <el-input v-model="quickForm.notes" placeholder="（可选）连接位置 / 用途" clearable />
            </el-form-item>
          </el-form>
        </el-tab-pane>
      </el-tabs>
      <!-- dialog-level footer: always visible, switches by active tab -->
      <template #footer>
        <div v-if="pickerTab === 'existing'">
          <el-button @click="pickerOpen = false">取消</el-button>
          <el-button type="primary" :disabled="!pickerDeviceId" @click="submitPicker">
            添加并连接
          </el-button>
        </div>
        <div v-else>
          <el-button @click="pickerOpen = false">取消</el-button>
          <el-button type="primary" :loading="quickBusy" @click="submitQuickConnect">
            创建并连接
          </el-button>
        </div>
      </template>
    </el-dialog>

    <!-- ── in-picker device edit dialog ────────────────────
         Edits the device's mutable metadata (name / hostname /
         model / role / location / group / notes / tags / SN /
         software version). Vendor and endpoints are intentionally
         NOT editable from here — vendor can't be re-pointed, and
         endpoints have their own management UI in the device
         detail page. -->
    <el-dialog
      v-model="editDialogOpen"
      title="编辑设备"
      width="560px"
      align-center
      :close-on-click-modal="!editBusy"
      @close="cancelEdit"
    >
      <el-form label-width="100px" size="small">
        <el-form-item label="名称" required>
          <el-input v-model="editForm.name" placeholder="设备名称" clearable />
        </el-form-item>
        <el-form-item label="主机名 / IP">
          <el-input v-model="editForm.hostname" placeholder="例如 10.0.0.1 或 core-sw-01" clearable />
        </el-form-item>
        <el-form-item label="角色">
          <el-input v-model="editForm.role" placeholder="switch / router / firewall …" clearable />
        </el-form-item>
        <el-form-item label="型号">
          <el-input v-model="editForm.model" placeholder="例如 S5735-S24T4X" clearable />
        </el-form-item>
        <el-form-item label="软件版本">
          <el-input v-model="editForm.software_version" placeholder="例如 V200R022C10" clearable />
        </el-form-item>
        <el-form-item label="序列号">
          <el-input v-model="editForm.serial_no" placeholder="SN" clearable />
        </el-form-item>
        <el-form-item label="位置">
          <el-input v-model="editForm.location" placeholder="例如 北京 · 机房 A · 42U" clearable />
        </el-form-item>
        <el-form-item label="分组">
          <el-input v-model="editForm.group_name" placeholder="例如 core / access / tor" clearable />
        </el-form-item>
        <el-form-item label="标签">
          <el-input v-model="editForm.tags" placeholder="逗号分隔" clearable />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="editForm.notes" type="textarea" :rows="3" placeholder="（可选）" />
        </el-form-item>
        <el-form-item label="厂商">
          <el-tag size="small" effect="plain" type="info">{{ editVendorLabel }}</el-tag>
          <span class="hint" style="margin-left:8px">厂商不可在此修改</span>
        </el-form-item>
        <el-form-item v-if="editError" label=" ">
          <el-text type="danger" size="small">{{ editError }}</el-text>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="cancelEdit" :disabled="editBusy">取消</el-button>
        <el-button type="primary" :loading="editBusy" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.multi-shell-page { height: 100%; }
.layout-card :deep(.el-card__body) { padding: 0; }

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 18px;
  border-bottom: 1px solid var(--border-1);
}
.header .title {
  font-weight: 600;
  font-size: 16px;
  display: inline-flex;
  align-items: center;
  gap: 10px;
}

.tab-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 10px 16px 0;
  border-bottom: 1px solid var(--border-1);
  background: var(--bg-canvas);
}
.tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px 6px 10px;
  background: var(--bg-input);
  border: 1px solid var(--border-1);
  border-bottom: none;
  border-top-left-radius: 6px;
  border-top-right-radius: 6px;
  cursor: pointer;
  font-size: 12.5px;
  max-width: 220px;
  transition: all 0.15s;
}
.tab:hover { border-color: var(--accent); }
.tab.active {
  background: var(--bg-canvas);
  border-color: var(--accent);
  font-weight: 600;
}
.tab.tab-broadcast {
  border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent) inset;
}
.tab-broadcast-check { margin-right: 2px; }
.tab-name {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}
.tab-name-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100px;
}
.tab-close { padding: 0 2px; }

.split {
  display: flex;
  height: calc(100vh - 280px);
  min-height: 480px;
  background: #0d1117;
}
.terminal-pane {
  flex: 1;
  position: relative;
  min-width: 0;
  background: #0d1117;
}
.log-pane {
  width: 360px;
  border-left: 1px solid var(--border-1);
  display: flex;
  flex-direction: column;
  background: var(--bg-canvas);
  min-height: 0;
}
.log-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 12px;
  border-bottom: 1px solid var(--border-1);
  flex-wrap: wrap;
  gap: 8px;
}
.log-title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  font-size: 13px;
  flex-wrap: wrap;
  min-width: 0;
}
.log-title-text {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 140px;
}
.log-header-actions {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}
.echo-local-toggle {
  margin-right: 6px;
  font-size: 12px;
}
.echo-local-toggle :deep(.el-checkbox__label) {
  font-size: 12px;
  color: var(--text-2);
}
.log-message {
  padding: 6px 12px;
  font-size: 12px;
  background: rgba(245, 108, 108, 0.08);
  border-bottom: 1px solid rgba(245, 108, 108, 0.2);
}

/* ── collapsible options panel (was the cramped .log-toolbar +
     .broadcast-bar). Each opt-row is a single labelled control group
     so things don't all stack on one line. */
.log-options {
  border-bottom: 1px solid var(--border-1);
  background: var(--bg-input);
}
.log-options :deep(.el-collapse-item__header) {
  padding: 0 12px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-2);
  background: transparent;
  border-bottom: none;
  min-height: 36px;
}
.log-options :deep(.el-collapse-item__wrap) {
  border-bottom: none;
  background: transparent;
}
.log-options :deep(.el-collapse-item__content) {
  padding: 4px 12px 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.opt-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-2);
  flex-wrap: wrap;
}
.opt-label {
  color: var(--text-3);
  min-width: 36px;
  font-weight: 500;
}
.opt-x { color: var(--text-3); }
.opt-mono { font-family: var(--font-mono); font-size: 11px; color: var(--text-3); min-width: 36px; }
.hint-icon { color: var(--text-3); font-size: 14px; }

/* The in-picker edit dialog uses .hint standalone (no parent class),
   so we need a global rule here. Matches the inline context in the
   dialog template ("厂商不可在此修改"). */
.hint { color: var(--text-3); font-size: 12px; }

/* ── destructive confirm button ──
   Used by the in-picker delete dialog. Element Plus's default
   ``el-button--danger`` paints white-on-red, which reads as
   washed-out / disabled.  We want a clear red word on a white
   surface so the operator can read "删除" without squinting, while
   still signalling "this is destructive" via the red border.

   The class is applied via ElMessageBox.confirm's
   ``confirmButtonClass`` option, which lands on a button rendered
   into a teleported Element Plus dialog — outside the host page's
   scoped stylesheet.  So we declare this rule globally (no
   ``scoped``) and let Element Plus's own element selectors do the
   targeting. */
.el-button.naw-danger-text {
  color: var(--danger, #ef4444);
  background: #ffffff;
  border: 1px solid var(--danger, #ef4444);
}
.el-button.naw-danger-text:hover {
  color: #ffffff;
  background: var(--danger, #ef4444);
  border-color: var(--danger, #ef4444);
}

.log-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px 12px 16px;
}
.empty {
  color: var(--text-3);
  font-size: 12px;
  text-align: center;
  padding: 24px 4px;
}
.log-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 4px;
  border-bottom: 1px dashed var(--border-1);
  font-size: 12.5px;
}
.log-row:hover { background: var(--bg-input); }
.log-row-draft {
  background: rgba(64, 158, 255, 0.06);
  border-left: 2px solid var(--accent);
  padding-left: 6px;
  font-style: italic;
}
.log-time {
  font-family: Menlo, Consolas, monospace;
  font-size: 11px;
  color: var(--text-3);
  flex-shrink: 0;
  width: 60px;
}
.log-text {
  flex: 1;
  font-family: Menlo, Consolas, monospace;
  font-size: 12.5px;
  color: var(--text-1);
  /* Read-only mirror — never looks clickable. */
  cursor: default;
  user-select: text;
  word-break: break-all;
  background: transparent;
}
.log-marker {
  color: var(--text-3);
  font-size: 12px;
  flex-shrink: 0;
  user-select: none;
}
</style>
