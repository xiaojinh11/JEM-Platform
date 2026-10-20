import { defineStore } from 'pinia'
import { Terminal } from 'xterm'
import { api } from '@/api'

export interface SSHSession {
  deviceId: number
  deviceName: string
  ws: WebSocket
  terminal: Terminal | null
  status: 'connecting' | 'connected' | 'disconnected' | 'error'
  statusMessage: string
  sessionId: string | null
  createdAt: Date
  lastActiveAt: Date
  endpointId: number | null
  credentialId: number | null
}

interface ConnectOptions {
  endpointId: number | null
  credentialId: number | null
}

export const useWebSSHStore = defineStore('webssh', {
  state: () => ({
    sessions: new Map<number, SSHSession>(),
    activeDeviceId: null as number | null,
    maxSessions: 6,
  }),

  getters: {
    activeSession: (state): SSHSession | null => {
      return state.activeDeviceId ? state.sessions.get(state.activeDeviceId) ?? null : null
    },
    sessionCount: (state): number => state.sessions.size,
    canAddSession: (state): boolean => state.sessions.size < state.maxSessions,
    sessionList: (state): SSHSession[] => Array.from(state.sessions.values())
      .sort((a, b) => b.lastActiveAt.getTime() - a.lastActiveAt.getTime()),
  },

  actions: {
    async connectDevice(
      deviceId: number,
      deviceName: string,
      options: ConnectOptions
    ): Promise<SSHSession> {
      // Check existing connection
      if (this.sessions.has(deviceId)) {
        const existing = this.sessions.get(deviceId)!
        if (existing.ws.readyState === WebSocket.OPEN) {
          console.log(`[WebSSH] Device ${deviceId} already connected, switching to it`)
          this.activeDeviceId = deviceId
          existing.lastActiveAt = new Date()
          return existing
        }
        console.log(`[WebSSH] Device ${deviceId} has stale connection, cleaning up`)
        this.disconnectDevice(deviceId)
      }

      // Check session limit
      if (!this.canAddSession) {
        throw new Error(`最多同时连接 ${this.maxSessions} 个设备`)
      }

      console.log(`[WebSSH] Connecting to device ${deviceId} (${deviceName})`, options)

      // Create WebSocket connection
      const ws = api.openWebSsh(deviceId, options)

      const session: SSHSession = {
        deviceId,
        deviceName,
        ws,
        terminal: null,
        status: 'connecting',
        statusMessage: '正在连接...',
        sessionId: null,
        createdAt: new Date(),
        lastActiveAt: new Date(),
        endpointId: options.endpointId,
        credentialId: options.credentialId,
      }

      // WebSocket event handlers
      ws.onopen = () => {
        console.log(`[WebSSH] Device ${deviceId} connected`)
        session.status = 'connected'
        session.statusMessage = '已连接'
        session.lastActiveAt = new Date()
      }

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data)
          
          // Extract session metadata
          if (msg.type === 'info') {
            if (msg.session_id) {
              session.sessionId = msg.session_id
              console.log(`[WebSSH] Device ${deviceId} session_id: ${msg.session_id}`)
            }
          }
          
          session.lastActiveAt = new Date()
          // Terminal output handled by component
        } catch (err) {
          console.error(`[WebSSH] Device ${deviceId} failed to parse message:`, err)
        }
      }

      ws.onclose = (ev) => {
        console.log(`[WebSSH] Device ${deviceId} disconnected (code=${ev.code})`)
        session.status = 'disconnected'
        session.statusMessage = `已断开 (${ev.code})`
        
        // Auto-remove from sessions map
        this.sessions.delete(deviceId)
        
        // Switch to most recently active session
        if (this.activeDeviceId === deviceId) {
          const remaining = this.sessionList
          this.activeDeviceId = remaining[0]?.deviceId ?? null
          console.log(`[WebSSH] Switched active device to ${this.activeDeviceId}`)
        }
      }

      ws.onerror = () => {
        console.error(`[WebSSH] Device ${deviceId} WebSocket error`)
        session.status = 'error'
        session.statusMessage = '连接错误'
      }

      this.sessions.set(deviceId, session)
      this.activeDeviceId = deviceId
      
      return session
    },

    disconnectDevice(deviceId: number): void {
      const session = this.sessions.get(deviceId)
      if (!session) {
        console.warn(`[WebSSH] Device ${deviceId} not found in sessions`)
        return
      }

      console.log(`[WebSSH] Disconnecting device ${deviceId}`)

      try {
        if (session.ws.readyState === WebSocket.OPEN) {
          session.ws.send(JSON.stringify({ type: 'close' }))
        }
        session.ws.close()
      } catch (err) {
        console.error(`[WebSSH] Failed to close device ${deviceId}:`, err)
      }

      this.sessions.delete(deviceId)

      // Switch active device if needed
      if (this.activeDeviceId === deviceId) {
        const remaining = Array.from(this.sessions.keys())
        this.activeDeviceId = remaining[0] ?? null
      }
    },

    setActiveDevice(deviceId: number): void {
      if (this.sessions.has(deviceId)) {
        console.log(`[WebSSH] Switching to device ${deviceId}`)
        this.activeDeviceId = deviceId
        const session = this.sessions.get(deviceId)!
        session.lastActiveAt = new Date()
      } else {
        console.warn(`[WebSSH] Cannot switch to device ${deviceId} - not in sessions`)
      }
    },

    disconnectAll(): void {
      console.log(`[WebSSH] Disconnecting all ${this.sessions.size} sessions`)
      for (const deviceId of this.sessions.keys()) {
        this.disconnectDevice(deviceId)
      }
    },

    updateSessionTerminal(deviceId: number, terminal: Terminal): void {
      const session = this.sessions.get(deviceId)
      if (session) {
        session.terminal = terminal
      }
    },
  },
})
