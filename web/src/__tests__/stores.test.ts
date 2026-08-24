import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useHealthStore } from '@/stores/health'
import { jsonResponse } from './httpMocks'

beforeEach(() => {
  setActivePinia(createPinia())
})

describe('health store', () => {
  it('starts unknown', () => {
    const store = useHealthStore()
    expect(store.status).toBe('unknown')
    expect(store.checkedAt).toBeNull()
  })

  it('check catches fetch failure and sets unhealthy', async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new Error('down'))
    const store = useHealthStore()
    await store.check()
    expect(store.status).toBe('unhealthy')
    expect(store.checkedAt).not.toBeNull()
  })

  it('check sets healthy on /health 200', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({ status: 'healthy' }))
    const store = useHealthStore()
    await store.check()
    expect(store.status).toBe('healthy')
  })

  it('check sets unhealthy on /health non-200', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({ detail: 'boom' }, { ok: false, status: 503 }))
    const store = useHealthStore()
    await store.check()
    expect(store.status).toBe('unhealthy')
  })
})
