import { beforeEach, describe, expect, it, vi } from 'vitest'
import { operationsApi } from '@/api/operations'

beforeEach(() => {
  vi.restoreAllMocks()
})

function mockJson(payload: unknown = {}) {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue({
    ok: true,
    json: () => Promise.resolve(payload)
  } as Response)
}

function requestedUrl(): URL {
  const [url] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]
  return new URL(String(url), 'http://ledger.test')
}

describe('operationsApi', () => {
  it('queries daily PnL with the Shanghai calendar boundary by default', async () => {
    mockJson([])

    await operationsApi.dailyPnl({
      account_id: 'testnet',
      strategy_id: 'spike-short',
      start_date: '2026-08-01',
      end_date: '2026-08-31'
    })

    const url = requestedUrl()
    expect(url.pathname).toBe('/api/v1/pnl/daily')
    expect(Object.fromEntries(url.searchParams)).toEqual({
      timezone: 'Asia/Shanghai',
      account_id: 'testnet',
      strategy_id: 'spike-short',
      start_date: '2026-08-01',
      end_date: '2026-08-31'
    })
  })

  it('allows the caller to request UTC daily boundaries explicitly', async () => {
    mockJson([])

    await operationsApi.dailyPnl({
      account_id: 'testnet',
      start_date: '2026-08-01',
      end_date: '2026-08-01',
      timezone: 'UTC'
    })

    expect(requestedUrl().searchParams.get('timezone')).toBe('UTC')
  })

  it('passes campaign-level performance filters without inventing defaults', async () => {
    mockJson({ total_trades: 0 })

    await operationsApi.performance({
      account_id: 'testnet',
      symbol: 'BTCUSDT',
      start_date: '2026-08-01',
      end_date: '2026-08-15'
    })

    const url = requestedUrl()
    expect(url.pathname).toBe('/api/v1/performance')
    expect(Object.fromEntries(url.searchParams)).toEqual({
      account_id: 'testnet',
      symbol: 'BTCUSDT',
      start_date: '2026-08-01',
      end_date: '2026-08-15'
    })
  })

  it('queries authoritative performance breakdown dimensions in Shanghai time', async () => {
    mockJson({ dimension_available: true, items: [] })

    await operationsApi.performanceBreakdown({
      account_id: 'testnet',
      strategy_id: 'spike-short',
      start_date: '2026-08-01',
      end_date: '2026-08-15',
      timezone: 'Asia/Shanghai',
      group_by: 'subcategory'
    })

    const url = requestedUrl()
    expect(url.pathname).toBe('/api/v1/performance/breakdown')
    expect(Object.fromEntries(url.searchParams)).toEqual({
      account_id: 'testnet',
      strategy_id: 'spike-short',
      start_date: '2026-08-01',
      end_date: '2026-08-15',
      timezone: 'Asia/Shanghai',
      group_by: 'subcategory'
    })
  })

  it('uses separate order, position and trade ledger routes', async () => {
    mockJson({ items: [], total: 0, limit: 50, offset: 0 })

    await operationsApi.orders({ account_id: 'a', status: 'NEW', limit: 50 })
    expect(requestedUrl().pathname).toBe('/api/v1/orders')
    expect(requestedUrl().searchParams.get('status')).toBe('NEW')

    vi.mocked(globalThis.fetch).mockClear()
    await operationsApi.orders({ account_id: 'a', active_only: true, limit: 25, offset: 25 })
    expect(Object.fromEntries(requestedUrl().searchParams)).toEqual({
      account_id: 'a', active_only: 'true', limit: '25', offset: '25'
    })

    vi.mocked(globalThis.fetch).mockClear()
    await operationsApi.positions({ account_id: 'a', strategy_id: 's' })
    expect(requestedUrl().pathname).toBe('/api/v1/positions')

    vi.mocked(globalThis.fetch).mockClear()
    await operationsApi.trades({ account_id: 'a', symbol: 'BTCUSDT', start_date: '2026-08-16', end_date: '2026-08-16', timezone: 'Asia/Shanghai' })
    expect(requestedUrl().pathname).toBe('/api/v1/trades')
    expect(requestedUrl().searchParams.get('start_date')).toBe('2026-08-16')
    expect(requestedUrl().searchParams.get('timezone')).toBe('Asia/Shanghai')
  })

  it('encodes category keys and paginates server-side category symbols', async () => {
    mockJson({ items: [], total: 0, limit: 20, offset: 40 })

    await operationsApi.categorySymbols('binance:subcategory:Layer 1', {
      limit: 20,
      offset: 40
    })

    const [rawUrl] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(String(rawUrl)).toContain(
      '/exchange-categories/binance%3Asubcategory%3ALayer%201/symbols'
    )
    expect(Object.fromEntries(requestedUrl().searchParams)).toEqual({
      limit: '20',
      offset: '40'
    })
  })

  it('queries unclassified exchange symbols from the authoritative backend filter', async () => {
    mockJson({ items: [], total: 0, limit: 1000, offset: 0 })

    await operationsApi.exchangeSymbols({ unclassified: true, limit: 1000 })

    const url = requestedUrl()
    expect(url.pathname).toBe('/api/v1/exchange-symbols')
    expect(Object.fromEntries(url.searchParams)).toEqual({
      unclassified: 'true',
      limit: '1000'
    })
  })

  it('requests the backend effective-universe preview with explicit filters', async () => {
    mockJson({ items: [] })

    await operationsApi.universePreview('spike/short', {
      freeze_days: 7,
      effective: false,
      limit: 100,
      offset: 0
    })

    const [rawUrl] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(String(rawUrl)).toContain(
      '/strategy-category-admissions/spike%2Fshort/universe-preview'
    )
    expect(Object.fromEntries(requestedUrl().searchParams)).toEqual({
      freeze_days: '7',
      effective: 'false',
      limit: '100',
      offset: '0'
    })
  })

  it('updates strategy admission with encoded identifiers and optimistic version', async () => {
    mockJson({ enabled: false, version: 4 })
    const update = {
      enabled: false,
      expected_version: 3,
      updated_by: 'web-operator',
      reason: '暂停新开仓'
    }

    await operationsApi.updateStrategyAdmission(
      'spike/short',
      'binance:category:Layer 1',
      update
    )

    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(String(url)).toContain(
      '/strategy-category-admissions/spike%2Fshort/binance%3Acategory%3ALayer%201'
    )
    expect(init).toEqual(expect.objectContaining({
      method: 'PUT',
      body: JSON.stringify(update)
    }))
  })

  it('queries sync status from its dedicated endpoint', async () => {
    mockJson({ initialized: true, status: 'ready' })

    await operationsApi.symbolSyncStatus()

    expect(requestedUrl().pathname).toBe('/api/v1/exchange-symbol-sync/status')
  })

  it('propagates API failures instead of returning placeholder data', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: false,
      status: 503,
      json: () => Promise.resolve({ detail: 'database unavailable' })
    } as Response)

    await expect(operationsApi.health()).rejects.toEqual(
      expect.objectContaining({
        status: 503,
        message: 'database unavailable'
      })
    )
  })
})
