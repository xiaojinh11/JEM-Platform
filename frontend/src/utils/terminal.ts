/**
 * Terminal data encoding utilities
 * Fixes deprecated unescape() usage and improves Backspace handling
 */

/**
 * Encode terminal input data for WebSocket transmission
 * @param data - Raw terminal input (may contain special chars like Backspace)
 * @returns Base64-encoded string
 */
export function encodeTerminalData(data: string): string {
  try {
    // Use TextEncoder for proper UTF-8 encoding (replaces deprecated unescape)
    const encoder = new TextEncoder()
    const bytes = encoder.encode(data)
    
    // Convert Uint8Array to binary string for btoa
    let binaryString = ''
    for (let i = 0; i < bytes.length; i++) {
      binaryString += String.fromCharCode(bytes[i])
    }
    
    return btoa(binaryString)
  } catch (err) {
    console.error('[Terminal] Encoding failed:', err, {
      data,
      charCodes: Array.from(data).map(c => c.charCodeAt(0))
    })
    throw new Error(`Terminal encoding failed: ${err}`)
  }
}

/**
 * Decode terminal output data from WebSocket
 * @param base64Data - Base64-encoded string from server
 * @returns Decoded string for terminal display
 */
export function decodeTerminalData(base64Data: string): string {
  try {
    const binaryString = atob(base64Data)
    // Convert binary string back to Uint8Array for proper UTF-8 decoding
    const bytes = new Uint8Array(binaryString.length)
    for (let i = 0; i < binaryString.length; i++) {
      bytes[i] = binaryString.charCodeAt(i)
    }
    const decoder = new TextDecoder('utf-8')
    return decoder.decode(bytes)
  } catch (err) {
    console.error('[Terminal] Decoding failed:', err, { base64Data })
    throw new Error(`Terminal decoding failed: ${err}`)
  }
}

/**
 * Debug helper to log special key presses
 * @param data - Terminal input data
 * @param deviceId - Device identifier for logging
 */
export function debugTerminalInput(data: string, deviceId: number): void {
  const charCode = data.charCodeAt(0)
  
  // Log special control characters
  const specialKeys: Record<number, string> = {
    8: 'Backspace (BS)',
    9: 'Tab',
    13: 'Enter (CR)',
    27: 'Escape (ESC)',
    127: 'Delete (DEL)',
  }
  
  if (specialKeys[charCode]) {
    console.debug(`[Terminal ${deviceId}] Special key: ${specialKeys[charCode]} (charCode=${charCode})`)
  } else if (charCode < 32) {
    // Other control characters
    console.debug(`[Terminal ${deviceId}] Control char: charCode=${charCode}`)
  }
}
