import { describe, it, expect, vi, beforeEach } from 'vitest'
import { api, ApiError } from '@/api/client'
import { backtestApi } from '@/api/backtests'

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('api client', () => {
  it('ApiError carries status and server detail', () => {
    const err = new ApiError(409, 'version conflict')
    expect(err.status).toBe(409)
    expect(err.message).toBe('version conflict')
    expect(err.name).toBe('ApiError')
  })

  it('get builds query string and fetches', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ total: 42 })
    } as Response)

    const result = await api.get('/orders', { symbol: 'BTCUSDT' })
    expect(result).toEqual({ total: 42 })
    // GET 除了 query 不传 method 字段，只带 headers。
    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(url).toContain('/api/v1/orders?symbol=BTCUSDT')
    expect(init).not.toHaveProperty('method')
    expect(init).toHaveProperty('headers', { 'Content-Type': 'application/json' })
  })

  it('get drops null/undefined/empty query values', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ total: 0 })
    } as Response)

    await api.get('/positions', { account_id: null, strategy_id: '', symbol: undefined })
    expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/positions'),
      expect.anything()
    )
  })

  it('backtest candles includes research id for archive resolution', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ symbol: 'AKEUSDT', interval: '5m', source: 'archive', candles: [] })
    } as Response)

    await backtestApi.candles({
      research_id: 'research-7', symbol: 'AKEUSDT', interval: '5m',
      start_ms: 1000, end_ms: 2000, source: 'archive'
    })

    const [url] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(url).toContain('research_id=research-7')
    expect(url).toContain('source=archive')
  })

  it('allows Binance market candles without a backtest research', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ symbol: 'BTCUSDT', interval: '5m', source: 'binance', candles: [] })
    } as Response)

    await backtestApi.candles({
      symbol: 'BTCUSDT', interval: '5m', start_ms: 1000, end_ms: 2000, source: 'binance'
    })

    const [url] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(url).toContain('source=binance')
    expect(url).not.toContain('research_id=')
  })

  it('put sends JSON body', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ subcategory: 'a', enabled: false })
    } as Response)

    const body = { enabled: false, expected_version: 2, updated_by: 'op', reason: null }
    await api.put('/subcategory-admissions/a', body)
    expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/subcategory-admissions/a'),
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify(body)
      })
    )
  })

  it('throws ApiError on non-ok response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: false,
      status: 503,
      json: () => Promise.resolve({ detail: 'unavailable' })
    } as Response)

    await expect(api.get('/health')).rejects.toThrow(ApiError)
    await expect(api.get('/health')).rejects.toMatchObject({ status: 503, message: 'unavailable' })
  })

  it('falls back to HTTP status when detail is missing', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: false,
      status: 502,
      json: () => Promise.resolve({})
    } as Response)

    await expect(api.get('/health')).rejects.toMatchObject({ message: 'HTTP 502' })
  })
})
