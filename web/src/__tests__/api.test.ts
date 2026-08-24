import { describe, it, expect, vi, beforeEach } from 'vitest'
import { api, ApiError } from '@/api/client'
import { backtestApi } from '@/api/backtests'
import { emptyResponse, jsonResponse, textResponse } from './httpMocks'

beforeEach(() => {
  vi.restoreAllMocks()
})

function requestedUrl(): string {
  return (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][0] as string
}

describe('api client', () => {
  it('ApiError carries status and server detail', () => {
    const err = new ApiError(409, 'version conflict')
    expect(err.status).toBe(409)
    expect(err.message).toBe('version conflict')
    expect(err.name).toBe('ApiError')
  })

  it('get builds query string and fetches', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ total: 42 }))

    const result = await api.get('/orders', { symbol: 'BTCUSDT' })
    expect(result).toEqual({ total: 42 })
    // GET 除了 query 不传 method 字段，只带 headers。
    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(url).toContain('/api/v1/orders?symbol=BTCUSDT')
    expect(init).not.toHaveProperty('method')
    expect(init).toHaveProperty('headers', { 'Content-Type': 'application/json' })
  })

  it('get drops null/undefined/empty query values', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ total: 0 }))

    await api.get('/positions', { account_id: null, strategy_id: '', symbol: undefined })
    expect(requestedUrl()).toBe('/api/v1/positions')
  })

  it('backtest candles includes research id for archive resolution', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ symbol: 'AKEUSDT', interval: '5m', source: 'archive', candles: [] })
    )

    await backtestApi.candles({
      research_id: 'research-7', symbol: 'AKEUSDT', interval: '5m',
      start_ms: 1000, end_ms: 2000, source: 'archive'
    })

    expect(requestedUrl()).toContain('research_id=research-7')
    expect(requestedUrl()).toContain('source=archive')
  })

  it('allows Binance market candles without a backtest research', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ symbol: 'BTCUSDT', interval: '5m', source: 'binance', candles: [] })
    )

    await backtestApi.candles({
      symbol: 'BTCUSDT', interval: '5m', start_ms: 1000, end_ms: 2000, source: 'binance'
    })

    expect(requestedUrl()).toContain('source=binance')
    expect(requestedUrl()).not.toContain('research_id=')
  })

  it('put sends JSON body', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ subcategory: 'a', enabled: false }))

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
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ detail: 'unavailable' }, { ok: false, status: 503 })
    )

    await expect(api.get('/health')).rejects.toThrow(ApiError)
    await expect(api.get('/health')).rejects.toMatchObject({ status: 503, message: 'unavailable' })
  })

  it('falls back to HTTP status when detail is missing', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({}, { ok: false, status: 502 }))

    await expect(api.get('/health')).rejects.toMatchObject({ message: 'HTTP 502' })
  })

  it('falls back to HTTP status when an error response has no body at all', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(emptyResponse(500))

    await expect(api.get('/health')).rejects.toMatchObject({ status: 500, message: 'HTTP 500' })
  })

  it('resolves DELETE without a response body instead of throwing on empty JSON', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(emptyResponse(204))

    await expect(api.delete<void>('/exchange-categories/a', { expected_version: 1 })).resolves.toBeUndefined()
  })

  it('treats a 200 with an empty body as no content', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(emptyResponse(200))

    await expect(api.delete<void>('/notifications/groups/g-1')).resolves.toBeUndefined()
  })

  it('reports an unparsable success body as an ApiError rather than leaking SyntaxError', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(textResponse('<html>gateway</html>'))

    await expect(api.get('/health')).rejects.toThrow(ApiError)
    await expect(api.get('/health')).rejects.toMatchObject({ message: '服务端返回了无法解析的响应体' })
  })
})
