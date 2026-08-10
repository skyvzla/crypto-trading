import { flushPromises, mount } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import BacktestResearchListView from '@/views/backtests/BacktestResearchListView.vue'
import BacktestReportDetailView from '@/views/backtests/BacktestReportDetailView.vue'
import BacktestTradeReplayView from '@/views/backtests/BacktestTradeReplayView.vue'
import { backtestApi } from '@/api/backtests'
import { router } from '@/router'

vi.mock('@/api/backtests', () => ({
  backtestApi: {
    researches: vi.fn(),
    report: vi.fn(),
    trade: vi.fn(),
    events: vi.fn(),
    strategySchema: vi.fn(),
    candles: vi.fn()
  }
}))

function plugins(path = '/') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return router.push(path).then(() => ({ list: [[VueQueryPlugin, { queryClient }]] as const }))
}

beforeEach(() => vi.clearAllMocks())

describe('回测关键视图', () => {
  it('研究记录只展示概要并提供两个线性入口', async () => {
    vi.mocked(backtestApi.researches).mockResolvedValue({
      items: [{ id: 'r-1', name: '7月全币种参数研究', strategy_id: 'spike-short', status: 'completed', trade_count: 1007, symbol_count: 494, win_rate: 0.67, net_pnl: 3304.57, created_at: '2026-08-10T08:00:00Z' }],
      total: 1, limit: 25, offset: 0
    })
    const { list } = await plugins()
    const wrapper = mount(BacktestResearchListView, { global: { plugins: list as never } })
    await flushPromises()
    expect(wrapper.text()).toContain('7月全币种参数研究')
    const links = wrapper.findAll('a').map((item) => item.attributes('href'))
    expect(links.some((href) => href?.endsWith('/backtests/r-1/reports'))).toBe(true)
    expect(links.some((href) => href?.endsWith('/backtests/r-1/symbols'))).toBe(true)
  })

  it('报表详情按后端 columns 动态生成表头和数据', async () => {
    vi.mocked(backtestApi.report).mockResolvedValue({
      descriptor: { type: 'pnl_bucket', title: '盈亏金额分组' },
      columns: [{ key: 'bucket', title: '盈亏区间' }, { key: 'trade_count', title: '交易数', type: 'number' }],
      rows: [{ bucket: '盈利大于10U', trade_count: 18 }], total: 1, limit: 50, offset: 0
    })
    const { list } = await plugins('/backtests/r-1/reports/pnl_bucket')
    const wrapper = mount(BacktestReportDetailView, { global: { plugins: list as never } })
    await flushPromises()
    expect(wrapper.text()).toContain('盈亏金额分组')
    expect(wrapper.text()).toContain('盈亏区间')
    expect(wrapper.text()).toContain('盈利大于10U')
  })

  it('单笔复盘按1500根窗口加载，并在退出定位时重新以退出时间取数', async () => {
    vi.mocked(backtestApi.trade).mockResolvedValue({
      id: 't-1', symbol: 'AKEUSDT', strategy_id: 'spike-short', entry_time: 1_750_000_000_000,
      entry_price: 1.1, exit_time: 1_750_001_800_000, exit_price: 1.2, net_pnl: -10,
      fills: [{ id: 'f-1', time: 1_750_000_000_000, price: 1.1 }]
    })
    vi.mocked(backtestApi.events).mockResolvedValue({ items: [] })
    vi.mocked(backtestApi.strategySchema).mockResolvedValue(null)
    vi.mocked(backtestApi.candles).mockResolvedValue({
      symbol: 'AKEUSDT', interval: '5m', source: 'binance',
      candles: [{ time: 1_750_000_000, open: 1, high: 1.2, low: 0.9, close: 1.1, volume: 10 }]
    })
    const { list } = await plugins('/backtests/r-1/trades/t-1')
    const wrapper = mount(BacktestTradeReplayView, {
      global: { plugins: list as never, stubs: { TradeCandlestickChart: true } }
    })
    await flushPromises()
    expect(backtestApi.candles).toHaveBeenLastCalledWith(expect.objectContaining({
      start_ms: 1_749_775_000_000,
      end_ms: 1_750_225_000_000
    }))
    await wrapper.get('.chart-tool-button:nth-of-type(2)').trigger('click')
    await flushPromises()
    expect(backtestApi.candles).toHaveBeenLastCalledWith(expect.objectContaining({
      start_ms: 1_749_776_800_000,
      end_ms: 1_750_226_800_000
    }))
  })
})
