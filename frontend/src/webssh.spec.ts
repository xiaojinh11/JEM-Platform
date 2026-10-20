import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useWebSSHStore } from '@/stores/webssh'

// Mock WebSocket
class MockWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  static CLOSING = 2
  static CLOSED = 3

  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null

  constructor(public url: string) {
    setTimeout(() => {
      this.readyState = MockWebSocket.OPEN
      this.onopen?.(new Event('open'))
    }, 0)
  }

  send(data: string): void {
    // Mock send
  }

  close(): void {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.(new CloseEvent('close', { code: 1000 }))
  }
}

// Mock api.openWebSsh
vi.mock('@/api', () => ({
  api: {
    openWebSsh: vi.fn((deviceId: number, _options: unknown) => {
      return new MockWebSocket(`ws://test/webssh/${deviceId}`) as unknown as WebSocket
    })
  }
}))

describe('WebSSH Store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    global.WebSocket = MockWebSocket as typeof WebSocket
  })

  it('should initialize with empty sessions', () => {
    const store = useWebSSHStore()
    expect(store.sessionCount).toBe(0)
    expect(store.activeDeviceId).toBeNull()
    expect(store.canAddSession).toBe(true)
  })

  it('should connect to a device', async () => {
    const store = useWebSSHStore()
    
    const session = await store.connectDevice(1, 'Device-1', {
      endpointId: 10,
      credentialId: 20
    })

    await vi.waitFor(() => {
      expect(store.sessionCount).toBe(1)
      expect(store.activeDeviceId).toBe(1)
      expect(session.deviceId).toBe(1)
      expect(session.deviceName).toBe('Device-1')
      expect(session.status).toBe('connected')
    })
  })

  it('should enforce max session limit', async () => {
    const store = useWebSSHStore()
    
    // Connect 6 devices
    for (let i = 1; i <= 6; i++) {
      await store.connectDevice(i, `Device-${i}`, {
        endpointId: 10,
        credentialId: 20
      })
    }

    await vi.waitFor(() => {
      expect(store.sessionCount).toBe(6)
      expect(store.canAddSession).toBe(false)
    })

    // 7th connection should fail
    await expect(
      store.connectDevice(7, 'Device-7', {
        endpointId: 10,
        credentialId: 20
      })
    ).rejects.toThrow('最多同时连接 6 个设备')
  })

  it('should switch to existing connection when reconnecting', async () => {
    const store = useWebSSHStore()
    
    // First connection
    await store.connectDevice(1, 'Device-1', {
      endpointId: 10,
      credentialId: 20
    })

    await vi.waitFor(() => {
      expect(store.sessions.get(1)?.status).toBe('connected')
    })

    const initialCount = store.sessionCount

    // Try to connect again - should not create new session
    await store.connectDevice(1, 'Device-1', {
      endpointId: 10,
      credentialId: 20
    })

    expect(store.sessionCount).toBe(initialCount)
    expect(store.activeDeviceId).toBe(1)
  })

  it('should disconnect a device', async () => {
    const store = useWebSSHStore()
    
    await store.connectDevice(1, 'Device-1', {
      endpointId: 10,
      credentialId: 20
    })

    await vi.waitFor(() => {
      expect(store.sessionCount).toBe(1)
    })

    store.disconnectDevice(1)

    await vi.waitFor(() => {
      expect(store.sessionCount).toBe(0)
      expect(store.activeDeviceId).toBeNull()
    })
  })

  it('should switch active device on disconnect', async () => {
    const store = useWebSSHStore()
    
    // Connect 2 devices
    await store.connectDevice(1, 'Device-1', { endpointId: 10, credentialId: 20 })
    await store.connectDevice(2, 'Device-2', { endpointId: 10, credentialId: 20 })

    await vi.waitFor(() => {
      expect(store.sessionCount).toBe(2)
      expect(store.activeDeviceId).toBe(2)
    })

    // Disconnect active device
    store.disconnectDevice(2)

    await vi.waitFor(() => {
      expect(store.sessionCount).toBe(1)
      expect(store.activeDeviceId).toBe(1)
    })
  })

  it('should disconnect all sessions', async () => {
    const store = useWebSSHStore()
    
    // Connect 3 devices
    for (let i = 1; i <= 3; i++) {
      await store.connectDevice(i, `Device-${i}`, {
        endpointId: 10,
        credentialId: 20
      })
    }

    await vi.waitFor(() => {
      expect(store.sessionCount).toBe(3)
    })

    store.disconnectAll()

    await vi.waitFor(() => {
      expect(store.sessionCount).toBe(0)
      expect(store.activeDeviceId).toBeNull()
    })
  })

  it('should sort sessions by last active time', async () => {
    const store = useWebSSHStore()
    
    await store.connectDevice(1, 'Device-1', { endpointId: 10, credentialId: 20 })
    await new Promise(resolve => setTimeout(resolve, 10))
    await store.connectDevice(2, 'Device-2', { endpointId: 10, credentialId: 20 })
    await new Promise(resolve => setTimeout(resolve, 10))
    await store.connectDevice(3, 'Device-3', { endpointId: 10, credentialId: 20 })

    await vi.waitFor(() => {
      expect(store.sessionCount).toBe(3)
    })

    // Switch to device 1 (should update lastActiveAt)
    await new Promise(resolve => setTimeout(resolve, 10))
    store.setActiveDevice(1)

    const sessionList = store.sessionList
    expect(sessionList[0].deviceId).toBe(1) // Most recent
    expect(sessionList[1].deviceId).toBe(3) // Middle
    expect(sessionList[2].deviceId).toBe(2) // Oldest
  })
})
