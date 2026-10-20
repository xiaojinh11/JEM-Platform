// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from 'vitest'
import { httpUpload } from './api'

describe('upload HTTP client', () => {
  beforeEach(() => localStorage.clear())

  it('adds the saved bearer token through the public interceptor API', async () => {
    localStorage.setItem('naw.token', 'upload-token')

    const response = await httpUpload.get('/probe', {
      adapter: async (config) => ({
        data: null,
        status: 200,
        statusText: 'OK',
        headers: {},
        config,
      }),
    })

    expect(response.config.headers.Authorization).toBe('Bearer upload-token')
  })
})
