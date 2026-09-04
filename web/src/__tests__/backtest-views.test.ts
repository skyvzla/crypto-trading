import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import BacktestResearchListView from '@/views/backtests/BacktestResearchListView.vue'
import BacktestReportDetailView from '@/views/backtests/BacktestReportDetailView.vue'
import BacktestEquityReplayView from '@/views/backtests/BacktestEquityReplayView.vue'
import BacktestSymbolListView from '@/views/backtests/BacktestSymbolListView.vue'
import BacktestTradeListView from '@/views/backtests/BacktestTradeListView.vue'
import BacktestTradeReplayView from '@/views/backtests/BacktestTradeReplayView.vue'
import TradeReplayChartPanel from '@/features/backtests/TradeReplayChartPanel.vue'
import { backtestApi } from '@/api/backtests'
import { chartSettingsApi } from '@/api/chartSettings'
import {
  cloneChartIndicatorSettings,
  DEFAULT_CHART_INDICATOR_SETTINGS,
} from '@/features/backtests/chartIndicatorSettings'
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
    candles: vi.fn(),
  },
}))

vi.mock('@/api/chartSettings', () => ({
  chartSettingsApi: {
    get: vi.fn(),
    update: vi.fn(),
  },
}))

/** vue-query 已在 vitest.setup.ts 全局安装，这里只需把路由推到目标地址。 */
async function atRoute(path = '/') {
  await router.push(path)
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(chartSettingsApi.get).mockResolvedValue(cloneChartIndicatorSettings(DEFAULT_CHART_INDICATOR_SETTINGS))
})

describe('回测关键视图', () => {
  it('图表续页以已加载K线边界为中心扩展窗口', async () => {
    const settings = cloneChartIndicatorSettings(DEFAULT_CHART_INDICATOR_SETTINGS)
    settings.default_interval = '5m'
    vi.mocked(chartSettingsApi.get).mockResolvedValue(settings)
    vi.mocked(backtestApi.candles).mockResolvedValue({
      symbol: 'AKEUSDT',
      interval: '5m',
      source: 'binance',
      candles: [
        { time: 1_749_775_000, open: 1, high: 1.2, low: 0.9, close: 1.1, volume: 10 },
        { time: 1_750_224_700, open: 1.1, high: 1.3, low: 1, close: 1.2, volume: 12 },
      ],
    })
    const wrapper = mount(TradeReplayChartPanel, {
      props: {
        researchId: 'r-1',
        trade: {
          id: 't-1',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: 1_750_000_000_000,
          entry_price: 1.1,
          net_pnl: 1,
        },
      },
      global: {
        stubs: {
          TradeCandlestickChart: {
            emits: ['request-more'],
            template: '<button aria-label="request-after" @click="$emit(\'request-more\', \'after\')" />',
          },
        },
      },
    })
    await flushPromises()
    await wrapper.get('button[aria-label="request-after"]').trigger('click')
    await flushPromises()
    expect(backtestApi.candles).toHaveBeenLastCalledWith(
      expect.objectContaining({
        start_ms: 1_749_999_700_000,
        end_ms: 1_750_449_700_000,
      }),
    )
  })

  it('等待设置读取完成后按保存的默认周期首次请求K线', async () => {
    let resolveSettings!: (settings: typeof DEFAULT_CHART_INDICATOR_SETTINGS) => void
    vi.mocked(chartSettingsApi.get).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSettings = resolve
        }),
    )
    vi.mocked(backtestApi.candles).mockResolvedValue({
      symbol: 'AKEUSDT',
      interval: '1s',
      source: 'archive',
      candles: [],
    })

    const wrapper = mount(TradeReplayChartPanel, {
      props: {
        researchId: 'r-1',
        trade: {
          id: 't-default-interval',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: 1_750_000_000_000,
          entry_price: 1.1,
          net_pnl: 1,
        },
      },
    })
    await flushPromises()

    expect(wrapper.find('.chart-loading').exists()).toBe(true)
    expect(wrapper.find('.chart-loading').text()).toContain('加载图表设置')
    expect(wrapper.find('.query-empty').exists()).toBe(false)
    expect(wrapper.get('button[aria-label="配置技术指标"]').attributes('disabled')).toBeDefined()
    expect(backtestApi.candles).not.toHaveBeenCalled()

    resolveSettings(cloneChartIndicatorSettings(DEFAULT_CHART_INDICATOR_SETTINGS))
    await flushPromises()

    expect(backtestApi.candles).toHaveBeenCalledOnce()
    expect(backtestApi.candles).toHaveBeenCalledWith(expect.objectContaining({ interval: '1s', source: 'archive' }))
  })

  it('行情模式不支持1s时回退到1m', async () => {
    vi.mocked(backtestApi.candles).mockResolvedValue({
      symbol: 'AKEUSDT',
      interval: '1m',
      source: 'binance',
      candles: [],
    })

    mount(TradeReplayChartPanel, {
      props: {
        mode: 'market',
        trade: {
          id: 't-market-default',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: 1_750_000_000_000,
          entry_price: 1.1,
          net_pnl: 1,
        },
      },
    })
    await flushPromises()

    expect(backtestApi.candles).toHaveBeenCalledWith(expect.objectContaining({ interval: '1m', source: 'binance' }))
  })

  it('设置读取失败时使用本地1s默认周期', async () => {
    vi.mocked(chartSettingsApi.get).mockRejectedValue(new Error('settings unavailable'))
    vi.mocked(backtestApi.candles).mockResolvedValue({
      symbol: 'AKEUSDT',
      interval: '1s',
      source: 'archive',
      candles: [],
    })

    mount(TradeReplayChartPanel, {
      props: {
        researchId: 'r-1',
        trade: {
          id: 't-settings-fallback',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: 1_750_000_000_000,
          entry_price: 1.1,
          net_pnl: 1,
        },
      },
    })
    await flushPromises()

    await vi.waitFor(
      () => {
        expect(backtestApi.candles).toHaveBeenCalledWith(expect.objectContaining({ interval: '1s', source: 'archive' }))
      },
      { timeout: 2_000 },
    )
  })

  it('设置响应不会覆盖等待期间手动选择的周期', async () => {
    let resolveSettings!: (settings: typeof DEFAULT_CHART_INDICATOR_SETTINGS) => void
    vi.mocked(chartSettingsApi.get).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSettings = resolve
        }),
    )
    vi.mocked(backtestApi.candles).mockResolvedValue({
      symbol: 'AKEUSDT',
      interval: '15m',
      source: 'binance',
      candles: [],
    })

    const wrapper = mount(TradeReplayChartPanel, {
      props: {
        researchId: 'r-1',
        trade: {
          id: 't-user-interval',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: 1_750_000_000_000,
          entry_price: 1.1,
          net_pnl: 1,
        },
      },
    })
    await flushPromises()
    await wrapper.get('input[value="15m"]').trigger('change')

    const settings = cloneChartIndicatorSettings(DEFAULT_CHART_INDICATOR_SETTINGS)
    settings.default_interval = '5m'
    resolveSettings(settings)
    await flushPromises()

    expect(backtestApi.candles).toHaveBeenCalledOnce()
    expect(backtestApi.candles).toHaveBeenCalledWith(expect.objectContaining({ interval: '15m' }))
  })

  it('保存设置完成后不会覆盖保存期间手动选择的周期', async () => {
    let resolveUpdate!: (settings: typeof DEFAULT_CHART_INDICATOR_SETTINGS) => void
    vi.mocked(chartSettingsApi.update).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpdate = resolve
        }),
    )
    vi.mocked(backtestApi.candles).mockImplementation(async (params) => ({
      symbol: params.symbol,
      interval: params.interval,
      source: params.source,
      candles: [],
    }))
    const settingsToSave = cloneChartIndicatorSettings(DEFAULT_CHART_INDICATOR_SETTINGS)
    settingsToSave.default_interval = '5m'

    const wrapper = mount(TradeReplayChartPanel, {
      props: {
        researchId: 'r-1',
        trade: {
          id: 't-save-user-interval',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: 1_750_000_000_000,
          entry_price: 1.1,
          net_pnl: 1,
        },
      },
      global: {
        stubs: {
          ChartIndicatorSettingsModal: {
            emits: ['save'],
            setup(_: unknown, { emit }: { emit: (event: 'save', settings: typeof settingsToSave) => void }) {
              return { save: () => emit('save', settingsToSave) }
            },
            template: '<button class="save-settings-probe" @click="save">保存设置</button>',
          },
        },
      },
    })
    await flushPromises()
    await wrapper.get('.save-settings-probe').trigger('click')
    await wrapper.get('input[value="15m"]').trigger('change')
    await flushPromises()

    resolveUpdate(settingsToSave)
    await flushPromises()

    expect(backtestApi.candles).toHaveBeenLastCalledWith(expect.objectContaining({ interval: '15m' }))
  })

  it('保存设置时取消在途读取，迟到响应不会覆盖新默认周期', async () => {
    let resolveSettings!: (settings: typeof DEFAULT_CHART_INDICATOR_SETTINGS) => void
    vi.mocked(chartSettingsApi.get).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSettings = resolve
        }),
    )
    const savedSettings = cloneChartIndicatorSettings(DEFAULT_CHART_INDICATOR_SETTINGS)
    savedSettings.default_interval = '15m'
    vi.mocked(chartSettingsApi.update).mockResolvedValue(savedSettings)
    vi.mocked(backtestApi.candles).mockImplementation(async (params) => ({
      symbol: params.symbol,
      interval: params.interval,
      source: params.source,
      candles: [],
    }))

    const wrapper = mount(TradeReplayChartPanel, {
      props: {
        researchId: 'r-1',
        trade: {
          id: 't-settings-race',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: 1_750_000_000_000,
          entry_price: 1.1,
          net_pnl: 1,
        },
      },
      global: {
        stubs: {
          ChartIndicatorSettingsModal: {
            emits: ['save'],
            setup(_: unknown, { emit }: { emit: (event: 'save', settings: typeof savedSettings) => void }) {
              return { save: () => emit('save', savedSettings) }
            },
            template: '<button class="save-settings-probe" @click="save">保存设置</button>',
          },
        },
      },
    })
    await flushPromises()
    await wrapper.get('.save-settings-probe').trigger('click')
    await flushPromises()

    const staleSettings = cloneChartIndicatorSettings(DEFAULT_CHART_INDICATOR_SETTINGS)
    staleSettings.default_interval = '5m'
    resolveSettings(staleSettings)
    await flushPromises()

    expect(backtestApi.candles).toHaveBeenCalledOnce()
    expect(backtestApi.candles).toHaveBeenCalledWith(expect.objectContaining({ interval: '15m' }))
  })

  it('从服务端读取全局指标配置并传给行情图', async () => {
    const settings = cloneChartIndicatorSettings(DEFAULT_CHART_INDICATOR_SETTINGS)
    settings.main.ma.enabled = true
    settings.main.ma.lines[0].period = 7
    vi.mocked(chartSettingsApi.get).mockResolvedValue(settings)
    vi.mocked(backtestApi.candles).mockResolvedValue({
      symbol: 'AKEUSDT',
      interval: '1s',
      source: 'archive',
      candles: [{ time: 1_750_000_000, open: 1, high: 1.2, low: 0.9, close: 1.1, volume: 10 }],
    })

    const wrapper = mount(TradeReplayChartPanel, {
      props: {
        researchId: 'r-1',
        trade: {
          id: 't-settings',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: 1_750_000_000_000,
          entry_price: 1.1,
          net_pnl: 1,
        },
      },
      global: {
        stubs: {
          TradeCandlestickChart: {
            name: 'TradeCandlestickChart',
            props: ['indicatorSettings'],
            template: '<div class="indicator-settings-probe">{{ indicatorSettings.main.ma.lines[0].period }}</div>',
          },
          ChartIndicatorSettingsModal: true,
        },
      },
    })
    await flushPromises()

    expect(chartSettingsApi.get).toHaveBeenCalledOnce()
    expect(wrapper.get('.indicator-settings-probe').text()).toBe('7')
  })

  it('研究记录只展示概要并提供两个线性入口', async () => {
    vi.mocked(backtestApi.researches).mockResolvedValue({
      items: [
        {
          id: 'r-1',
          name: '7月全币种参数研究',
          strategy_id: 'spike-short',
          status: 'completed',
          trade_count: 1007,
          symbol_count: 494,
          win_rate: 0.67,
          net_pnl: 3304.57,
          created_at: '2026-08-10T08:00:00Z',
        },
      ],
      total: 1,
      limit: 25,
      offset: 0,
    })
    await atRoute()
    const wrapper = mount(BacktestResearchListView)
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
      columns: [
        { key: 'bucket', title: '盈亏区间' },
        { key: 'trade_count', title: '交易数', type: 'number' },
      ],
      rows: [{ bucket: '盈利大于10U', trade_count: 18 }],
      total: 1,
      limit: 50,
      offset: 0,
    })
    await atRoute('/backtests/r-1/reports/pnl_bucket')
    const wrapper = mount(BacktestReportDetailView)
    await flushPromises()
    expect(wrapper.text()).toContain('盈亏金额分组')
    expect(wrapper.text()).toContain('盈亏区间')
    expect(wrapper.text()).toContain('bucket')
    expect(wrapper.text()).toContain('trade_count')
    expect(wrapper.text()).toContain('盈利大于10U')
  })

  it('交易对列表从 URL 恢复筛选和服务端排序', async () => {
    vi.mocked(backtestApi.symbols).mockResolvedValue({
      items: [
        {
          symbol: 'AKEUSDT',
          trade_count: 2,
          win_rate: 0.5,
          net_pnl: -10,
          limit_order_fill_rate: null,
        },
      ],
      total: 1,
      limit: 25,
      offset: 0,
    })
    await atRoute('/backtests/r-1/symbols?symbol_filter=AKE&sort_by=win_rate&sort_order=asc')
    const wrapper = mount(BacktestSymbolListView)
    await flushPromises()
    expect(backtestApi.symbols).toHaveBeenCalledWith('r-1', 25, 0, 'AKE', 'win_rate', 'asc')
    expect((wrapper.get('input').element as HTMLInputElement).value).toBe('AKE')
    expect(wrapper.findAll('.ant-table-column-sorters').length).toBeGreaterThanOrEqual(8)
    expect(wrapper.text()).toContain('挂单成交率')
    expect(wrapper.text()).toContain('-')
  })

  it('交易对筛选无结果时仍可清除条件并恢复列表', async () => {
    vi.mocked(backtestApi.symbols).mockImplementation(async (_researchId, limit, offset, filter) => ({
      items: filter === 'NO_MATCH' ? [] : [{ symbol: 'AKEUSDT', trade_count: 2, win_rate: 0.5, net_pnl: -10 }],
      total: filter === 'NO_MATCH' ? 0 : 1,
      limit,
      offset,
    }))
    await atRoute('/backtests/r-1/symbols?symbol_filter=NO_MATCH')
    const wrapper = mount(BacktestSymbolListView)
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
      items: [
        {
          id: 't-1',
          symbol: 'AKEUSDT',
          entry_time: 1_750_000_000_000,
          entry_price: 1.1,
          exit_time: 1_750_001_800_000,
          exit_price: 1.2,
          net_pnl: -10,
          net_return: -0.1,
          winner: false,
          entry_fill_count: 2,
        },
      ],
      total: 1,
      limit: 25,
      offset: 0,
    })
    await atRoute(
      '/backtests/r-1/symbols/AKEUSDT/trades?result=loss&min_pnl=-100&trade_sort_by=net_pnl&trade_sort_order=asc',
    )
    const wrapper = mount(BacktestTradeListView)
    await flushPromises()
    expect(backtestApi.trades).toHaveBeenCalledWith(
      'r-1',
      'AKEUSDT',
      25,
      0,
      expect.objectContaining({
        winner: false,
        min_pnl: -100,
        sort_by: 'net_pnl',
        sort_order: 'asc',
      }),
    )
    expect(wrapper.find('input[placeholder="最低盈亏 U"]').exists()).toBe(true)
    expect(wrapper.findAll('.ant-table-column-sorters').length).toBeGreaterThanOrEqual(10)
    expect(wrapper.text()).toContain('入场成交笔数')
    expect(wrapper.text()).toContain('2')
  })

  it('收益曲线默认以500U资金池、500U储备和50%盈利复投回放', async () => {
    vi.mocked(backtestApi.replayParameterSets).mockResolvedValue({
      items: [{ parameters: {}, trade_count: 1, net_pnl: 50 }],
    })
    vi.mocked(backtestApi.replayTrades).mockResolvedValue({
      parameters: {},
      items: [
        {
          id: 't-1',
          symbol: 'AKEUSDT',
          entry_time: 1_750_000_000_000,
          exit_time: 1_750_001_800_000,
          entry_price: 1,
          exit_price: 0.9,
          net_pnl: 50,
          gross_pnl: 50,
          entry_notional: 500,
          gross_return: 0.1,
        },
      ],
    })
    await atRoute('/backtests/r-1/equity')
    const wrapper = mount(BacktestEquityReplayView, {
      global: { stubs: { EquityCurveChart: true } },
    })
    await flushPromises()

    const values = wrapper.findAll('input').map((input) => (input.element as HTMLInputElement).value)
    expect(values).toEqual(expect.arrayContaining(['1000.00', '500.00', '50.00']))
    expect(wrapper.text()).toContain('初始仓位')
    expect(wrapper.text()).toContain('盈利复投')
    expect(wrapper.find('.equity-value-fields').exists()).toBe(true)
    expect(wrapper.text()).toContain('交易资金池')
    expect(wrapper.text()).toContain('锁定储备')
    const replayButton = wrapper.find('button[aria-label="打开单笔K线复盘"]')
    expect(replayButton.exists()).toBe(true)
    expect(replayButton.element.closest('a')?.getAttribute('href')).toContain('/backtests/r-1/trades/t-1?from=equity')
  })

  it('从收益曲线打开单笔复盘时，返回按钮回到收益曲线', async () => {
    vi.mocked(backtestApi.trade).mockResolvedValue({
      id: 't-1',
      symbol: 'AKEUSDT',
      strategy_id: 'spike-short',
      entry_time: 1_750_000_000_000,
      entry_price: 1.1,
      exit_time: 1_750_001_800_000,
      exit_price: 1.2,
      net_pnl: -10,
      orders: [],
      fills: [],
    })
    vi.mocked(backtestApi.events).mockResolvedValue({ items: [] })
    vi.mocked(backtestApi.strategySchema).mockResolvedValue(null)
    vi.mocked(backtestApi.candles).mockResolvedValue({
      symbol: 'AKEUSDT',
      interval: '5m',
      source: 'binance',
      candles: [],
    })
    await atRoute('/backtests/r-1/trades/t-1?from=equity')
    const wrapper = mount(BacktestTradeReplayView, {
      global: { stubs: { TradeCandlestickChart: true } },
    })
    await flushPromises()

    await wrapper.get('button[aria-label="返回"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/backtests/r-1/equity')
  })

  it('单笔复盘按1500根窗口加载，并在退出定位时重新以退出时间取数', async () => {
    vi.mocked(backtestApi.trade).mockResolvedValue({
      id: 't-1',
      symbol: 'AKEUSDT',
      strategy_id: 'spike-short',
      entry_time: 1_750_000_000_000,
      entry_price: 1.1,
      exit_time: 1_750_001_800_000,
      exit_price: 1.2,
      net_pnl: -10.12345,
      trade_id: 'backtest-trade:AKEUSDT:1',
      campaign_id: 'spike_short:AKEUSDT:1750000000000',
      side: 'SHORT',
      signal_time: 1_749_999_000_000,
      signal_price: 1.05,
      invalid_price: 1.4,
      orders: [
        {
          id: 'order-entry-1',
          order_id: 'order-entry-1',
          symbol: 'AKEUSDT',
          side: 'SELL',
          order_type: 'LIMIT',
          type: 'LIMIT',
          price: 1.1,
          quantity: 3,
          filled_quantity: 1.5,
          avg_fill_price: 1.095,
          status: 'PARTIALLY_FILLED',
          reduce_only: false,
          created_time: 1_750_000_100_000,
          fill_time: 1_750_000_300_000,
        },
        {
          id: 'order-exit-1',
          order_id: 'order-exit-1',
          symbol: 'AKEUSDT',
          side: 'BUY',
          order_type: 'MARKET',
          type: 'MARKET',
          price: null,
          quantity: 1.5,
          filled_quantity: 1.5,
          avg_fill_price: 1.2,
          status: 'FILLED',
          reduce_only: true,
          created_time: 1_750_001_700_000,
          fill_time: 1_750_001_800_000,
        },
      ],
      fills: [
        {
          id: 'f-entry-1a',
          fill_id: 'f-entry-1a',
          order_id: 'order-entry-1',
          time: 1_750_000_200_000,
          price: 1.09,
          quantity: 0.5,
          commission: 0.001,
          commission_asset: 'USDT',
          is_maker: true,
          side: 'SELL',
        },
        {
          id: 'f-entry-1b',
          fill_id: 'f-entry-1b',
          order_id: 'order-entry-1',
          time: 1_750_000_300_000,
          price: 1.1,
          quantity: 1,
          commission: 0.002,
          commission_asset: 'USDT',
          is_maker: false,
          side: 'SELL',
        },
        {
          id: 'f-exit-1',
          fill_id: 'f-exit-1',
          order_id: 'order-exit-1',
          time: 1_750_001_800_000,
          price: 1.2,
          quantity: 1.5,
          commission: 0.003,
          commission_asset: 'USDT',
          is_maker: false,
          side: 'BUY',
        },
      ],
    })
    vi.mocked(backtestApi.events).mockResolvedValue({
      items: [
        {
          id: 1,
          time: 1_750_000_000_000,
          type: 'entry_plan_created',
          title: 'entry_plan_created',
          description: null,
          price: null,
          data: {
            rise_5s: '0.06',
            rise_threshold_5s: '0.05',
            tier_prices: ['1.1', '1.2', '1.3'],
            invalid_price: '1.4',
          },
        },
      ],
    })
    vi.mocked(backtestApi.strategySchema).mockResolvedValue(null)
    vi.mocked(backtestApi.candles).mockResolvedValue({
      symbol: 'AKEUSDT',
      interval: '1s',
      source: 'archive',
      candles: [{ time: 1_750_000_000, open: 1, high: 1.2, low: 0.9, close: 1.1, volume: 10 }],
    })
    await atRoute('/backtests/r-1/trades/t-1?symbol_filter=AKE&result=loss')
    const wrapper = mount(BacktestTradeReplayView, {
      global: { stubs: { TradeCandlestickChart: true } },
    })
    await flushPromises()
    expect(wrapper.find('.event-heading').text()).toContain('入场计划创建（entry_plan_created）')
    expect(wrapper.find('.trade-summary-strip').text()).toContain('-10.123 U')
    expect(wrapper.find('.event-heading time').text()).not.toBe('')
    expect(wrapper.find('.event-tables-grid').exists()).toBe(true)
    expect(wrapper.find('.event-group-card').exists()).toBe(false)
    expect(wrapper.findAll('.event-parameters tbody tr').length).toBe(3)
    expect(wrapper.findAll('.event-parameters thead th').length).toBe(3)
    expect(wrapper.find('.event-parameters').text()).toContain('参数 / 指标')
    expect(wrapper.find('.event-parameters').text()).toContain('参数值')
    expect(wrapper.find('.event-parameters').text()).toContain('门槛值')
    expect(wrapper.find('.event-parameters').text()).toContain('5.00%')
    expect(wrapper.find('.event-parameters tbody tr').classes()).toContain('is-major')
    expect(wrapper.text()).not.toContain('{"rise_5s"')
    expect(wrapper.text()).toContain('交易基准')
    expect(wrapper.text()).toContain('信号时间')
    expect(wrapper.text()).toContain('信号价格')
    expect(wrapper.text()).toContain('失效价格')
    expect(wrapper.text()).not.toContain('成交档位')
    expect(wrapper.text()).not.toContain('卖1')
    expect(wrapper.text()).not.toContain('限卖')
    expect(wrapper.text()).toContain('订单明细')
    expect(wrapper.find('.trade-summary-strip').text()).toContain('订单数2')
    expect(wrapper.find('.trade-summary-strip').text()).toContain('成交笔数3')
    expect(wrapper.text()).toContain('开仓/只减仓')
    expect(wrapper.text()).toContain('成交均价')
    expect(wrapper.text()).toContain('完成/最后成交时间')
    expect(wrapper.findAll('.orders-table .ant-table-tbody > .ant-table-row')).toHaveLength(2)
    expect(wrapper.find('.orders-table').text()).toContain('1.5')
    expect(wrapper.find('.orders-table').text()).toContain('PARTIALLY_FILLED')
    expect(wrapper.find('.orders-table').text()).toContain('只减仓')
    expect(wrapper.find('.orders-table').text()).toContain('MARKET')
    expect(wrapper.find('.orders-table').text()).toContain('order-entry-1')
    expect(wrapper.find('.orders-table').text()).toContain('order-exit-1')
    expect(wrapper.find('.fills-table').exists()).toBe(false)
    expect(wrapper.text()).toContain('交易 ID')
    expect(wrapper.text()).toContain('backtest-trade:AKEUSDT:1')
    expect(wrapper.text()).toContain('信号 ID')
    expect(wrapper.text()).toContain('spike_short:AKEUSDT:1750000000000')
    expect(wrapper.find('button[aria-label="标线显示"]').exists()).toBe(true)
    expect(wrapper.find('.line-visibility-menu').exists()).toBe(false)
    expect(wrapper.find('.ant-timeline-item-label').exists()).toBe(false)
    expect(wrapper.find('button[aria-label="返回"]').exists()).toBe(true)
    expect(backtestApi.candles).toHaveBeenLastCalledWith(
      expect.objectContaining({
        start_ms: 1_749_999_250_000,
        end_ms: 1_750_000_750_000,
        source: 'archive',
      }),
    )
    await wrapper.get('button[aria-label="跳转到退出成交"]').trigger('click')
    await flushPromises()
    expect(backtestApi.candles).toHaveBeenLastCalledWith(
      expect.objectContaining({
        start_ms: 1_750_001_050_000,
        end_ms: 1_750_002_550_000,
      }),
    )
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

  it('单笔复盘将成交明细、事件时间线和扩展参数纵向排列，并使成交字段按屏幕响应式分列', async () => {
    vi.mocked(backtestApi.trade).mockResolvedValue({
      id: 't-1',
      symbol: 'AKEUSDT',
      strategy_id: 'spike-short',
      entry_time: 1_750_000_000_000,
      entry_price: 1.1,
      signal_time: 1_750_000_000_000,
      signal_price: 1.1,
      invalid_price: 1.4,
      exit_time: 1_750_001_800_000,
      exit_price: 1.2,
      net_pnl: -10,
      tier_prices: [1.1],
      orders: [],
      fills: [],
    })
    vi.mocked(backtestApi.events).mockResolvedValue({
      items: [
        {
          id: 1,
          type: 'signal_triggered',
          title: 'signal_triggered',
          time: 1_750_000_000_000,
          price: 1.1,
          data: {},
        },
      ],
    })
    vi.mocked(backtestApi.strategySchema).mockResolvedValue({
      strategy_id: 'spike-short',
      fields: [{ key: 'rise_5s', label: '5 秒涨幅' }],
    })
    vi.mocked(backtestApi.candles).mockResolvedValue({
      symbol: 'AKEUSDT',
      interval: '5m',
      source: 'binance',
      candles: [],
    })
    await atRoute('/backtests/r-1/trades/t-1')
    const wrapper = mount(BacktestTradeReplayView, {
      global: { stubs: { TradeCandlestickChart: true } },
    })
    await flushPromises()

    expect(wrapper.find('.replay-details').exists()).toBe(false)
    expect(wrapper.find('.trade-details-section h3').text()).toBe('交易基准')
    expect(wrapper.find('.order-details-section h3').text()).toBe('订单明细')
    expect(wrapper.find('.timeline-section > h3').text()).toBe('事件时间线')
    expect(wrapper.find('.timeline-panel').exists()).toBe(true)
    expect(wrapper.find('.timeline-panel h3').exists()).toBe(false)
    expect(wrapper.find('.timeline-panel .timeline-events').exists()).toBe(true)
    expect(wrapper.find('.timeline-section + .detail-section h3').text()).toBe('策略扩展参数')

    const descriptions = wrapper.find('.trade-details-section').findComponent({ name: 'ADescriptions' })
    expect(descriptions.exists()).toBe(true)
    expect(descriptions.props('column')).toEqual({ xs: 1, sm: 2, md: 2, lg: 4, xl: 4, xxl: 6 })
  })

  it('单笔复盘展开订单时只展示同一 order_id 的部分成交明细', async () => {
    vi.mocked(backtestApi.trade).mockResolvedValue({
      id: 't-partial',
      symbol: 'AKEUSDT',
      strategy_id: 'spike-short',
      entry_time: 1_750_000_000_000,
      entry_price: 1.1,
      exit_time: 1_750_001_800_000,
      exit_price: 1.2,
      net_pnl: 2,
      orders: [
        {
          id: 'order-partial',
          order_id: 'order-partial',
          symbol: 'AKEUSDT',
          side: 'SELL',
          type: 'LIMIT',
          price: 1.1,
          quantity: 2,
          filled_quantity: 1,
          avg_fill_price: 1.095,
          status: 'PARTIALLY_FILLED',
          reduce_only: false,
          created_time: 1_750_000_100_000,
          fill_time: 1_750_000_300_000,
        },
        {
          id: 'order-other',
          order_id: 'order-other',
          symbol: 'AKEUSDT',
          side: 'BUY',
          type: 'MARKET',
          quantity: 1,
          filled_quantity: 1,
          avg_fill_price: 1.2,
          status: 'FILLED',
          reduce_only: true,
          created_time: 1_750_001_700_000,
          fill_time: 1_750_001_800_000,
        },
        {
          id: 'order-empty',
          order_id: 'order-empty',
          symbol: 'AKEUSDT',
          side: 'SELL',
          type: 'LIMIT',
          price: 1.15,
          quantity: 1,
          filled_quantity: 0,
          status: 'CANCELED',
          reduce_only: false,
          created_time: 1_750_000_100_000,
          cancel_time: 1_750_000_400_000,
        },
      ],
      fills: [
        {
          id: 'fill-partial-a',
          fill_id: 'fill-partial-a',
          order_id: 'order-partial',
          time: 1_750_000_200_000,
          price: 1.09,
          quantity: 0.4,
          commission: 0.001,
          commission_asset: 'USDT',
          is_maker: true,
        },
        {
          id: 'fill-partial-b',
          fill_id: 'fill-partial-b',
          order_id: 'order-partial',
          time: 1_750_000_300_000,
          price: 1.1,
          quantity: 0.6,
          commission: 0.002,
          commission_asset: 'USDT',
          is_maker: false,
        },
        {
          id: 'fill-other',
          fill_id: 'fill-other',
          order_id: 'order-other',
          time: 1_750_001_800_000,
          price: 1.2,
          quantity: 1,
          commission: 0.003,
          commission_asset: 'USDT',
          is_maker: false,
        },
      ],
    })
    vi.mocked(backtestApi.events).mockResolvedValue({ items: [] })
    vi.mocked(backtestApi.strategySchema).mockResolvedValue(null)
    vi.mocked(backtestApi.candles).mockResolvedValue({
      symbol: 'AKEUSDT',
      interval: '5m',
      source: 'binance',
      candles: [],
    })
    await atRoute('/backtests/r-1/trades/t-partial')
    const wrapper = mount(BacktestTradeReplayView, {
      global: { stubs: { TradeCandlestickChart: true } },
    })
    await flushPromises()

    const expandButtons = wrapper
      .findAll('.orders-table button[aria-label="Expand row"]')
      .filter((button) => !button.classes().includes('ant-table-row-expand-icon-spaced'))
    expect(expandButtons).toHaveLength(2)
    expect(
      wrapper.get('.orders-table tr[data-row-key="order-empty"] button[aria-label="Expand row"]').classes(),
    ).toContain('ant-table-row-expand-icon-spaced')
    const expandButton = expandButtons[0]
    expect(expandButton.exists()).toBe(true)
    await expandButton.trigger('click')
    await flushPromises()

    expect(wrapper.find('.fills-table').exists()).toBe(true)
    expect(wrapper.find('.fills-table').text()).toContain('fill-partial-a')
    expect(wrapper.find('.fills-table').text()).toContain('fill-partial-b')
    expect(wrapper.find('.fills-table').text()).not.toContain('fill-other')
    expect(wrapper.find('.fills-table').text()).toContain('Maker')
    expect(wrapper.find('.fills-table').text()).toContain('Taker')
    expect(wrapper.find('.fills-table').text()).toContain('0.001 USDT')
  })
})
