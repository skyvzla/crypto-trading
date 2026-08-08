import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useFiltersStore } from '@/stores/filters'
import { useHealthStore } from '@/stores/health'

beforeEach(() => {
  setActivePinia(createPinia())
})

describe('filters store', () => {
  it('starts with empty filters', () => {
    const store = useFiltersStore()
    expect(store.accountId).toBe('')
    expect(store.strategyId).toBe('')
    expect(store.symbol).toBe('')
  })

  it('query drops empty fields', () => {
    const store = useFiltersStore()
    expect(store.query).toEqual({})
    store.symbol = 'BTCUSDT'
    expect(store.query).toEqual({ symbol: 'BTCUSDT' })
    store.symbol = '  '
    expect(store.query).toEqual({})
  })

  it('reset clears all fields', () => {
    const store = useFiltersStore()
    store.accountId = 'acc1'
    store.strategyId = 's1'
    store.symbol = 'X'
    store.reset()
    expect(store.accountId).toBe('')
    expect(store.strategyId).toBe('')
    expect(store.symbol).toBe('')
  })
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
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ status: 'healthy' })
    })
    const store = useHealthStore()
    await store.check()
    expect(store.status).toBe('healthy')
  })

  it('check sets unhealthy on /health non-200', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: false,
      json: () => Promise.resolve({ detail: 'boom' })
    })
    const store = useHealthStore()
    await store.check()
    expect(store.status).toBe('unhealthy')
  })
})
