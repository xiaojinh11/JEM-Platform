import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { encodeTerminalData, decodeTerminalData, debugTerminalInput } from '@/utils/terminal'

describe('Terminal Utilities', () => {
  describe('encodeTerminalData', () => {
    it('should encode ASCII text correctly', () => {
      const result = encodeTerminalData('hello')
      expect(result).toBe(btoa('hello'))
    })

    it('should encode UTF-8 text correctly', () => {
      const result = encodeTerminalData('你好')
      // UTF-8 encoding: 你好 → e4bda0 e5a5bd
      const encoder = new TextEncoder()
      const bytes = encoder.encode('你好')
      const expected = btoa(String.fromCharCode(...bytes))
      expect(result).toBe(expected)
    })

    it('should encode Backspace (DEL) correctly', () => {
      const result = encodeTerminalData('\x7F')
      expect(result).toBe(btoa('\x7F'))
    })

    it('should encode Backspace (BS) correctly', () => {
      const result = encodeTerminalData('\x08')
      expect(result).toBe(btoa('\x08'))
    })

    it('should encode special control characters', () => {
      // Tab, Enter, Escape
      expect(encodeTerminalData('\t')).toBe(btoa('\t'))
      expect(encodeTerminalData('\r')).toBe(btoa('\r'))
      expect(encodeTerminalData('\x1B')).toBe(btoa('\x1B'))
    })

    it('should encode mixed text and control chars', () => {
      const input = 'ls -la\r'
      const result = encodeTerminalData(input)
      expect(result).toBe(btoa('ls -la\r'))
    })

    it('should throw on encoding failure', () => {
      // Mock TextEncoder to throw
      const originalEncoder = global.TextEncoder
      global.TextEncoder = class {
        encode() {
          throw new Error('Encoding failed')
        }
      } as typeof TextEncoder

      expect(() => encodeTerminalData('test')).toThrow('Terminal encoding failed')

      global.TextEncoder = originalEncoder
    })
  })

  describe('decodeTerminalData', () => {
    it('should decode Base64 correctly', () => {
      const input = btoa('hello')
      expect(decodeTerminalData(input)).toBe('hello')
    })

    it('should decode ANSI escape sequences', () => {
      const ansiText = '\x1b[32mGreen\x1b[0m'
      const encoded = btoa(ansiText)
      expect(decodeTerminalData(encoded)).toBe(ansiText)
    })

    it('should throw on invalid Base64', () => {
      expect(() => decodeTerminalData('invalid!!!base64')).toThrow('Terminal decoding failed')
    })
  })

  describe('debugTerminalInput', () => {
    let consoleDebugSpy: ReturnType<typeof vi.spyOn>

    beforeEach(() => {
      consoleDebugSpy = vi.spyOn(console, 'debug').mockImplementation(() => {})
    })

    afterEach(() => {
      consoleDebugSpy.mockRestore()
    })

    it('should log Backspace (BS) char code 8', () => {
      debugTerminalInput('\x08', 1)
      expect(consoleDebugSpy).toHaveBeenCalledWith(
        '[Terminal 1] Special key: Backspace (BS) (charCode=8)'
      )
    })

    it('should log Delete (DEL) char code 127', () => {
      debugTerminalInput('\x7F', 2)
      expect(consoleDebugSpy).toHaveBeenCalledWith(
        '[Terminal 2] Special key: Delete (DEL) (charCode=127)'
      )
    })

    it('should log Tab char code 9', () => {
      debugTerminalInput('\t', 3)
      expect(consoleDebugSpy).toHaveBeenCalledWith(
        '[Terminal 3] Special key: Tab (charCode=9)'
      )
    })

    it('should log Enter (CR) char code 13', () => {
      debugTerminalInput('\r', 4)
      expect(consoleDebugSpy).toHaveBeenCalledWith(
        '[Terminal 4] Special key: Enter (CR) (charCode=13)'
      )
    })

    it('should log Escape char code 27', () => {
      debugTerminalInput('\x1B', 5)
      expect(consoleDebugSpy).toHaveBeenCalledWith(
        '[Terminal 5] Special key: Escape (ESC) (charCode=27)'
      )
    })

    it('should log other control characters', () => {
      debugTerminalInput('\x01', 6) // Ctrl+A
      expect(consoleDebugSpy).toHaveBeenCalledWith(
        '[Terminal 6] Control char: charCode=1'
      )
    })

    it('should not log normal printable characters', () => {
      debugTerminalInput('a', 7)
      expect(consoleDebugSpy).not.toHaveBeenCalled()
    })
  })

  describe('Integration: encode → decode round trip', () => {
    it('should preserve ASCII text', () => {
      const original = 'display version\r\n'
      const encoded = encodeTerminalData(original)
      const decoded = decodeTerminalData(encoded)
      expect(decoded).toBe(original)
    })

    it('should preserve UTF-8 text', () => {
      const original = '显示版本信息'
      const encoded = encodeTerminalData(original)
      const decoded = decodeTerminalData(encoded)
      expect(decoded).toBe(original)
    })

    it('should preserve Backspace sequences', () => {
      const original = 'typo\x7F\x7F\x7Ftest'
      const encoded = encodeTerminalData(original)
      const decoded = decodeTerminalData(encoded)
      expect(decoded).toBe(original)
    })

    it('should preserve ANSI colors', () => {
      const original = '\x1b[31mRed\x1b[0m \x1b[32mGreen\x1b[0m'
      const encoded = encodeTerminalData(original)
      const decoded = decodeTerminalData(encoded)
      expect(decoded).toBe(original)
    })
  })
})
