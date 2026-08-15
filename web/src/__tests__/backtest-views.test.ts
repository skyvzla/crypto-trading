import { flushPromises, mount } from '@vue/test-utils'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import BacktestResearchListView from '@/views/backtests/BacktestResearchListView.vue'
import BacktestReportDetailView from '@/views/backtests/BacktestReportDetailView.vue'
import BacktestEquityReplayView from '@/views/backtests/BacktestEquityReplayView.vue'
import BacktestSymbolListView from '@/views/backtests/BacktestSymbolListView.vue'
import BacktestTradeListView from '@/views/backtests/BacktestTradeListView.vue'
import BacktestTradeReplayView from '@/views/backtests/BacktestTradeReplayView.vue'
import { backtestApi } from '@/api/backtests'
import { router } from '@/router'

vi.mock('@/api/backtests', () => ({
  backtestApi: {
    researches: vi.fn(),
    replayParameterSets: vi.fn(),
    replayTrades: vi.fn(),
    report: vi.fn(),
    symbols: vi.fn(),
    trades: vi.fn(),
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
    expect(links.some((href) => href?.endsWith('/backtests/r-1/equity'))).toBe(true)
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
    expect(wrapper.text()).toContain('bucket')
    expect(wrapper.text()).toContain('trade_count')
    expect(wrapper.text()).toContain('盈利大于10U')
  })

  it('交易对列表从 URL 恢复筛选和服务端排序', async () => {
    vi.mocked(backtestApi.symbols).mockResolvedValue({
      items: [{ symbol: 'AKEUSDT', trade_count: 2, win_rate: 0.5, net_pnl: -10 }], total: 1, limit: 25, offset: 0
    })
    const { list } = await plugins('/backtests/r-1/symbols?symbol_filter=AKE&sort_by=win_rate&sort_order=asc')
    const wrapper = mount(BacktestSymbolListView, { global: { plugins: list as never } })
    await flushPromises()
    expect(backtestApi.symbols).toHaveBeenCalledWith('r-1', 25, 0, 'AKE', 'win_rate', 'asc')
    expect((wrapper.get('input').element as HTMLInputElement).value).toBe('AKE')
    expect(wrapper.findAll('.ant-table-column-sorters').length).toBeGreaterThanOrEqual(8)
  })

  it('交易对筛选无结果时仍可清除条件并恢复列表', async () => {
    vi.mocked(backtestApi.symbols).mockImplementation(async (_researchId, limit, offset, filter) => ({
      items: filter === 'NO_MATCH' ? [] : [{ symbol: 'AKEUSDT', trade_count: 2, win_rate: 0.5, net_pnl: -10 }],
      total: filter === 'NO_MATCH' ? 0 : 1,
      limit,
      offset
    }))
    const { list } = await plugins('/backtests/r-1/symbols?symbol_filter=NO_MATCH')
    const wrapper = mount(BacktestSymbolListView, { global: { plugins: list as never } })
    await flushPromises()

    expect(wrapper.find('.query-empty').exists()).toBe(true)
    const clearButton = wrapper.findAll('button').find((button) => button.text() === '清除筛选')
    expect(clearButton).toBeDefined()
    await clearButton!.trigger('click')
    await flushPromises()

    expect(backtestApi.symbols).toHaveBeenLastCalledWith('r-1', 25, 0, '', 'net_pnl', 'desc')
    expect(router.currentRoute.value.query.symbol_filter).toBeUndefined()
    expect(wrapper.text()).toContain('AKEUSDT')
  })

  it('交易明细从 URL 恢复筛选并提供主要字段排序', async () => {
    vi.mocked(backtestApi.trades).mockResolvedValue({
      items: [{ id: 't-1', symbol: 'AKEUSDT', entry_time: 1_750_000_000_000, entry_price: 1.1, exit_time: 1_750_001_800_000, exit_price: 1.2, net_pnl: -10, net_return: -0.1, winner: false }],
      total: 1, limit: 25, offset: 0
    })
    const { list } = await plugins('/backtests/r-1/symbols/AKEUSDT/trades?result=loss&min_pnl=-100&trade_sort_by=net_pnl&trade_sort_order=asc')
    const wrapper = mount(BacktestTradeListView, { global: { plugins: list as never } })
    await flushPromises()
    expect(backtestApi.trades).toHaveBeenCalledWith('r-1', 'AKEUSDT', 25, 0, expect.objectContaining({
      winner: false, min_pnl: -100, sort_by: 'net_pnl', sort_order: 'asc'
    }))
    expect(wrapper.find('input[placeholder="最低盈亏 U"]').exists()).toBe(true)
    expect(wrapper.findAll('.ant-table-column-sorters').length).toBeGreaterThanOrEqual(10)
  })

  it('收益曲线默认以500U资金池、500U储备和50%盈利复投回放', async () => {
    vi.mocked(backtestApi.replayParameterSets).mockResolvedValue({
      items: [{ parameters: {}, trade_count: 1, net_pnl: 50 }]
    })
    vi.mocked(backtestApi.replayTrades).mockResolvedValue({
      parameters: {},
      items: [{ id: 't-1', symbol: 'AKEUSDT', entry_time: 1_750_000_000_000, exit_time: 1_750_001_800_000, entry_price: 1, exit_price: 0.9, net_pnl: 50, gross_pnl: 50, entry_notional: 500, gross_return: 0.1 }]
    })
    const { list } = await plugins('/backtests/r-1/equity')
    const wrapper = mount(BacktestEquityReplayView, {
      global: { plugins: list as never, stubs: { EquityCurveChart: true } }
    })
    await flushPromises()

    const values = wrapper.findAll('input').map((input) => (input.element as HTMLInputElement).value)
    expect(values).toEqual(expect.arrayContaining(['1000.00', '500.00', '50.00']))
    expect(wrapper.text()).toContain('初始仓位')
    expect(wrapper.text()).toContain('盈利复投')
    expect(wrapper.text()).toContain('交易资金池')
    expect(wrapper.text()).toContain('锁定储备')
  })

  it('单笔复盘按1500根窗口加载，并在退出定位时重新以退出时间取数', async () => {
    vi.mocked(backtestApi.trade).mockResolvedValue({
      id: 't-1', symbol: 'AKEUSDT', strategy_id: 'spike-short', entry_time: 1_750_000_000_000,
      entry_price: 1.1, exit_time: 1_750_001_800_000, exit_price: 1.2, net_pnl: -10,
      side: 'SHORT', tier_prices: [1.1, 1.2, 1.3],
      fills: [
        { id: 'f-1', time: 1_750_000_000_000, price: 1.1, side: 'SELL' },
        { id: 'f-exit', time: 1_750_001_800_000, price: 1.2, side: 'BUY' }
      ]
    })
    vi.mocked(backtestApi.events).mockResolvedValue({
      items: [{ id: 1, time: 1_750_000_000_000, type: 'entry_plan_created', title: 'entry_plan_created', description: null, price: null, data: { tier_prices: ['1.1', '1.2', '1.3'] } }]
    })
    vi.mocked(backtestApi.strategySchema).mockResolvedValue(null)
    vi.mocked(backtestApi.candles).mockResolvedValue({
      symbol: 'AKEUSDT', interval: '5m', source: 'binance',
      candles: [{ time: 1_750_000_000, open: 1, high: 1.2, low: 0.9, close: 1.1, volume: 10 }]
    })
    const { list } = await plugins('/backtests/r-1/trades/t-1?symbol_filter=AKE&result=loss')
    const wrapper = mount(BacktestTradeReplayView, {
      global: { plugins: list as never, stubs: { TradeCandlestickChart: true } }
    })
    await flushPromises()
    expect(wrapper.find('.event-heading').text()).toContain('entry_plan_created')
    expect(wrapper.find('.event-heading time').text()).not.toBe('')
    expect(wrapper.find('.event-content').text()).toContain('tier_prices')
    expect(wrapper.text()).toContain('已成交 1 / 3 档')
    expect(wrapper.text()).toContain('卖1')
    expect(wrapper.text()).toContain('限卖2')
    expect(wrapper.text()).toContain('触发 K线')
    expect(wrapper.text()).toContain('确认')
    expect(wrapper.find('button[aria-label="标线显示"]').exists()).toBe(true)
    expect(wrapper.find('.line-visibility-menu').exists()).toBe(false)
    expect(wrapper.find('.ant-timeline-item-label').exists()).toBe(false)
    expect(wrapper.find('button[aria-label="返回"]').exists()).toBe(true)
    expect(backtestApi.candles).toHaveBeenLastCalledWith(expect.objectContaining({
      start_ms: 1_749_775_000_000,
      end_ms: 1_750_225_000_000,
      source: 'binance'
    }))
    await wrapper.get('button[aria-label="跳转到退出成交"]').trigger('click')
    await flushPromises()
    expect(backtestApi.candles).toHaveBeenLastCalledWith(expect.objectContaining({
      start_ms: 1_749_776_800_000,
      end_ms: 1_750_226_800_000
    }))
    await wrapper.get('input[value="1s"]').trigger('change')
    await flushPromises()
    expect(backtestApi.candles).toHaveBeenLastCalledWith(expect.objectContaining({ source: 'archive', interval: '1s' }))
    await wrapper.get('button[aria-label="返回"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/backtests/r-1/symbols/AKEUSDT/trades')
    expect(router.currentRoute.value.query).toMatchObject({ symbol_filter: 'AKE', result: 'loss' })
    expect(backtestApi.trade).not.toHaveBeenCalledWith('r-1', '')
    expect(backtestApi.events).not.toHaveBeenCalledWith('r-1', '')
  })
})
