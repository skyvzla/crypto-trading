import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createChart } from 'lightweight-charts'
import TradeCandlestickChart from '@/features/backtests/TradeCandlestickChart.vue'
import {
  cloneChartIndicatorSettings,
  DEFAULT_CHART_INDICATOR_SETTINGS,
} from '@/features/backtests/chartIndicatorSettings'

const remove = vi.fn()
const setData = vi.fn()
const createPriceLine = vi.fn()
const removePriceLine = vi.fn()
const setVisibleLogicalRange = vi.fn()
const setVisibleRange = vi.fn()
const createSeriesMarkers = vi.fn()
const observe = vi.fn()
const disconnect = vi.fn()
const paneSetStretchFactor = vi.fn()
const subscribeCrosshairMove = vi.fn()
const subscribeVisibleLogicalRangeChange = vi.fn()
let visibleLogicalRange = { from: 0, to: 100 }
let logicalTimes: number[] = []
const seriesApis: Array<Record<string, unknown>> = []
const seriesOptions: Array<Record<string, unknown>> = []
const paneMocks = Array.from({ length: 8 }, (_, index) => ({
  getHeight: vi.fn(() => (index === 0 ? 300 : 100)),
  getStretchFactor: vi.fn(() => (index === 0 ? 3 : 1)),
  setStretchFactor: paneSetStretchFactor,
  getHTMLElement: vi.fn(() => ({ getBoundingClientRect: () => ({ top: index * 100 }) })),
}))

vi.mock('lightweight-charts', () => ({
  CandlestickSeries: {},
  HistogramSeries: {},
  LineSeries: {},
  ColorType: { Solid: 'solid' },
  CrosshairMode: { Normal: 0 },
  LineStyle: { Solid: 0, Dashed: 2, Dotted: 1 },
  MismatchDirection: { None: 0 },
  createSeriesMarkers: (...args: unknown[]) => createSeriesMarkers(...args),
  createChart: vi.fn(() => ({
    addSeries: (_definition: unknown, options: Record<string, unknown> = {}) => {
      const isFirstSeries = seriesApis.length === 0
      let ownData: Array<Record<string, unknown>> = []
      const api = {
        setData: (data: Array<Record<string, unknown>>) => {
          setData(data)
          ownData = data
          if (isFirstSeries) logicalTimes = data.map((point) => Number(point.time))
        },
        dataByIndex: (index: number) => {
          const time = logicalTimes[index]
          return ownData.find((point) => Number(point.time) === time) ?? null
        },
        // 真实 API 返回可移除的价格线句柄，标线显隐依赖它做就地增删。
        createPriceLine: (...args: unknown[]) => {
          createPriceLine(...args)
          return { applyOptions: vi.fn(), options: vi.fn() }
        },
        removePriceLine,
        priceToCoordinate: vi.fn((price: number) => price * 100),
      }
      seriesApis.push(api)
      seriesOptions.push(options)
      return api
    },
    timeScale: () => ({
      fitContent: vi.fn(),
      getVisibleRange: vi.fn(() => ({ from: 1_754_000_030, to: 1_754_000_060 })),
      getVisibleLogicalRange: vi.fn(() => visibleLogicalRange),
      setVisibleRange,
      setVisibleLogicalRange: (range: { from: number; to: number }) => {
        visibleLogicalRange = range
        setVisibleLogicalRange(range)
      },
      timeToCoordinate: vi.fn(() => 100),
      subscribeVisibleLogicalRangeChange: (handler: unknown) => subscribeVisibleLogicalRangeChange(handler),
      unsubscribeVisibleLogicalRangeChange: vi.fn(),
    }),
    priceScale: () => ({ applyOptions: vi.fn() }),
    panes: () => paneMocks,
    subscribeCrosshairMove: (handler: unknown) => subscribeCrosshairMove(handler),
    unsubscribeCrosshairMove: vi.fn(),
    applyOptions: vi.fn(),
    remove,
  })),
}))

beforeEach(() => {
  vi.clearAllMocks()
  seriesApis.length = 0
  seriesOptions.length = 0
  logicalTimes = []
  visibleLogicalRange = { from: 0, to: 100 }
  localStorage.clear()
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe = observe
      disconnect = disconnect
    },
  )
})

describe('TradeCandlestickChart', () => {
  it('绘制K线、关键价位，并在卸载时释放图表和观察器', async () => {
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles: [{ time: 1_754_000_000, open: 1, high: 1.2, low: 0.9, close: 1.1, volume: 10 }],
        trade: {
          id: 't-1',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: 1_754_000_000_000,
          entry_price: 1.1,
          average_entry_price: 1.1,
          invalid_price: 1.3,
          exit_time: 1_754_000_060_000,
          exit_price: 1.2,
          net_pnl: -10,
          tier_prices: [1.1, 1.15, 1.2],
        },
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    const chartOptions = vi.mocked(createChart).mock.calls.at(-1)?.[1] as {
      layout: { background: { color: string }; textColor: string }
      grid: { vertLines: { color: string }; horzLines: { color: string } }
    }
    expect(chartOptions.layout.background.color).toBe('#ffffff')
    expect(chartOptions.layout.textColor).toBe('#334155')
    expect(chartOptions.grid.vertLines.color).toBe('#e2e8f0')
    expect(chartOptions.grid.horzLines.color).toBe('#e2e8f0')
    expect(setData).toHaveBeenCalledOnce()
    expect(createPriceLine).toHaveBeenCalledTimes(4)
    expect(observe).toHaveBeenCalledOnce()
    wrapper.unmount()
    expect(disconnect).toHaveBeenCalled()
    expect(remove).toHaveBeenCalled()
  })

  it('默认以首笔成交为中心显示前后各30根，并支持跳转退出', async () => {
    const start = 1_754_000_000
    const candles = Array.from({ length: 101 }, (_, index) => ({
      time: start + index,
      open: 1,
      high: 1.2,
      low: 0.9,
      close: 1.1,
      volume: 10,
    }))
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles,
        focusTime: null,
        trade: {
          id: 't-2',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: (start + 40) * 1000,
          entry_price: 1.1,
          exit_time: (start + 80) * 1000,
          exit_price: 1.2,
          net_pnl: -10,
          fills: [{ id: 'f-1', time: (start + 40) * 1000, price: 1.1, tier: 1 }],
        },
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(setVisibleLogicalRange).toHaveBeenCalledWith({ from: 9, to: 69 })
    ;(wrapper.vm as unknown as { focusExit: () => void }).focusExit()
    expect(setVisibleLogicalRange).toHaveBeenLastCalledWith({ from: 49, to: 100 })
    expect(createSeriesMarkers).toHaveBeenCalled()
  })

  it('接近当前视窗边缘时预取，并在同方向数据接入后自动继续加载', async () => {
    const start = 1_754_000_000
    const candles = Array.from({ length: 300 }, (_, index) => ({
      time: start + index,
      open: 1,
      high: 1.2,
      low: 0.9,
      close: 1.1,
      volume: 10,
    }))
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles,
        trade: {
          id: 't-more',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: (start + 150) * 1000,
          entry_price: 1.1,
          net_pnl: 1,
        },
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    await new Promise((resolve) => setTimeout(resolve, 550))
    const rangeHandler = subscribeVisibleLogicalRangeChange.mock.calls.at(-1)?.[0] as (range: {
      from: number
      to: number
    }) => void
    rangeHandler({ from: 42, to: 102 })
    expect(wrapper.emitted('request-more')).toEqual([['before']])
    await wrapper.setProps({
      candles: [{ time: start - 1, open: 1, high: 1.2, low: 0.9, close: 1.1, volume: 10 }, ...candles],
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(wrapper.emitted('request-more')).toEqual([['before'], ['before']])
  })

  it('缩小到两侧都进入预取区时加载更近的右侧后续K线', async () => {
    const start = 1_754_000_000
    const candles = Array.from({ length: 300 }, (_, index) => ({
      time: start + index,
      open: 1,
      high: 1.2,
      low: 0.9,
      close: 1.1,
      volume: 10,
    }))
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles,
        trade: {
          id: 't-wide',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: (start + 150) * 1000,
          entry_price: 1.1,
          exit_time: (start + 302) * 1000,
          net_pnl: 1,
        },
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    await new Promise((resolve) => setTimeout(resolve, 550))
    const rangeHandler = subscribeVisibleLogicalRangeChange.mock.calls.at(-1)?.[0] as (range: {
      from: number
      to: number
    }) => void
    visibleLogicalRange = { from: 100, to: 650 }
    rangeHandler(visibleLogicalRange)
    expect(wrapper.emitted('request-more')).toEqual([['after']])
    await wrapper.setProps({
      candles: [
        ...candles,
        { time: start + 300, open: 1, high: 1.2, low: 0.9, close: 1.1, volume: 10 },
        { time: start + 301, open: 1, high: 1.2, low: 0.9, close: 1.1, volume: 10 },
      ],
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(wrapper.emitted('request-more')).toEqual([['after'], ['after']])
  })

  it('将回测成交确认时刻定位到实际触价的前一根1秒K线', async () => {
    const start = 1_754_000_000
    const candles = Array.from({ length: 61 }, (_, index) => ({
      time: start + index,
      open: 1,
      high: 1.2,
      low: 0.9,
      close: 1.1,
      volume: 10,
    }))
    mount(TradeCandlestickChart, {
      props: {
        candles,
        trade: {
          id: 't-fill-candle',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          side: 'SHORT',
          entry_time: (start + 40) * 1000,
          entry_price: 1.1,
          exit_time: (start + 50) * 1000,
          exit_price: 1,
          net_pnl: 1,
          tier_prices: [1.1, 1.15, 1.2],
          fills: [
            { id: 'f-1', time: (start + 40) * 1000, price: 1.1, tier: 1, side: 'SELL' },
            { id: 'f-2', time: (start + 41) * 1000, price: 1.15, tier: 2, side: 'SELL' },
            { id: 'f-exit', time: (start + 50) * 1000, price: 1.2, side: 'BUY' },
          ],
        },
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    const markers = createSeriesMarkers.mock.calls.at(-1)?.[1] as Array<{ time: number; text: string }>
    expect(markers).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ time: start + 39, text: '卖1' }),
        expect.objectContaining({ time: start + 40, text: '卖2' }),
        expect.objectContaining({ time: start + 49, text: '退出' }),
      ]),
    )
    expect(createPriceLine).toHaveBeenCalledWith(expect.objectContaining({ title: '卖1', lineWidth: 1 }))
    expect(createPriceLine).toHaveBeenCalledWith(expect.objectContaining({ title: '卖2', lineWidth: 1 }))
  })

  it('按真实交易所时间逐笔标记账本买卖成交', async () => {
    const start = 1_754_000_000
    const candles = Array.from({ length: 61 }, (_, index) => ({
      time: start + index,
      open: 1,
      high: 1.2,
      low: 0.9,
      close: 1.1,
      volume: 10,
    }))
    mount(TradeCandlestickChart, {
      props: {
        candles,
        fillDisplay: 'all',
        fillTimeSemantics: 'exchange',
        trade: {
          id: 'campaign-chart',
          symbol: 'AKEUSDT',
          side: 'SHORT',
          entry_time: (start + 40) * 1000,
          entry_price: 1.1,
          exit_time: (start + 50) * 1000,
          exit_price: 1,
          net_pnl: 1,
          fills: [
            { id: 'buy-1', time: (start + 40) * 1000, price: 1.1, side: 'BUY' },
            { id: 'sell-1', time: (start + 41) * 1000, price: 1.15, side: 'SELL' },
            { id: 'buy-2', time: (start + 50) * 1000, price: 1.05, side: 'BUY' },
          ],
        },
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    const markers = createSeriesMarkers.mock.calls.at(-1)?.[1] as Array<{ time: number; text: string }>
    expect(markers).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ time: start + 40, text: '买1' }),
        expect.objectContaining({ time: start + 41, text: '卖1' }),
        expect.objectContaining({ time: start + 50, text: '买2' }),
      ]),
    )
    expect(markers.some((marker) => marker.text === '退出')).toBe(false)
  })

  it('成交价优先于同价限价，并以统一名称绘制未成交档位和极值标签', async () => {
    const start = 1_754_000_000
    mount(TradeCandlestickChart, {
      props: {
        candles: [
          { time: start, open: 1, high: 1.3, low: 0.9, close: 1.1, volume: 10 },
          { time: start + 1, open: 1.1, high: 1.2, low: 0.8, close: 1, volume: 12 },
        ],
        trade: {
          id: 't-prices',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          side: 'SHORT',
          entry_time: (start + 1) * 1000,
          entry_price: 1.1,
          invalid_price: 1.4,
          tier_prices: [1.1, 1.2, 1.3],
          net_pnl: 1,
          fills: [{ id: 'f-1', time: (start + 1) * 1000, price: 1.1, tier: 1, side: 'SELL' }],
        },
        overlays: [
          { key: 'invalid_price', label: '失效价', kind: 'price_line' },
          { key: 'spike_high', label: '尖峰高点', kind: 'price_line' },
        ],
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(createPriceLine).toHaveBeenCalledWith(expect.objectContaining({ title: '卖1', price: 1.1 }))
    expect(createPriceLine).toHaveBeenCalledWith(expect.objectContaining({ title: '限卖2', price: 1.2 }))
    expect(createPriceLine).toHaveBeenCalledWith(expect.objectContaining({ title: '限卖3', price: 1.3 }))
    expect(createPriceLine).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: '失效价', price: 1.4, color: undefined }),
    )
    expect(createPriceLine.mock.calls.filter(([line]) => (line as { title: string }).title === '失效价')).toHaveLength(
      1,
    )
    expect(createPriceLine.mock.calls.some(([line]) => (line as { title: string }).title === '尖峰高点')).toBe(false)
    const markers = createSeriesMarkers.mock.calls.at(-1)?.[1] as Array<{
      time: number
      position: string
      text: string
    }>
    expect(markers).toEqual(expect.arrayContaining([expect.objectContaining({ time: start, text: '卖1' })]))
    expect(markers.some((marker) => marker.text === '最高' || marker.text === '最低')).toBe(false)
  })

  it('极值显示为主题适配的价格文本，且可独立隐藏各类标线', async () => {
    const start = 1_754_000_000
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles: [
          { time: start, open: 1, high: 1.345, low: 0.9, close: 1.1, volume: 10 },
          { time: start + 1, open: 1.1, high: 1.2, low: 0.789, close: 1, volume: 12 },
        ],
        trade: {
          id: 't-lines',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          side: 'SHORT',
          entry_time: start * 1000,
          entry_price: 1.1,
          invalid_price: 1.4,
          tier_prices: [1.1, 1.2, 1.3],
          net_pnl: 1,
        },
        lineVisibility: { tiers: false },
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(createPriceLine.mock.calls.some(([line]) => /^限卖|^卖/.test((line as { title: string }).title))).toBe(false)
    expect(createPriceLine.mock.calls.every(([line]) => (line as { lineWidth: number }).lineWidth === 1)).toBe(true)
    const labels = wrapper.findAll('.extrema-price-label')
    expect(labels.map((label) => label.text())).toEqual(expect.arrayContaining(['1.345', '0.789']))
    expect(labels.every((label) => label.attributes('style')?.includes('rgb(51, 65, 85)'))).toBe(true)
  })

  it('追加K线时更新现有series并保留缩放和可视位置', async () => {
    const start = 1_754_000_000
    const candles = Array.from({ length: 80 }, (_, index) => ({
      time: start + index,
      open: 1,
      high: 1.2,
      low: 0.9,
      close: 1.1,
      volume: 10,
    }))
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles,
        trade: {
          id: 't-3',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: (start + 40) * 1000,
          entry_price: 1.1,
          net_pnl: 1,
        },
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    const removeCount = remove.mock.calls.length
    await wrapper.setProps({
      candles: [...candles, { time: start + 80, open: 1.1, high: 1.3, low: 1, close: 1.2, volume: 12 }],
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(remove).toHaveBeenCalledTimes(removeCount)
    expect(setData).toHaveBeenLastCalledWith(expect.arrayContaining([expect.objectContaining({ time: start + 80 })]))
    expect(setVisibleRange).toHaveBeenCalledWith({ from: 1_754_000_030, to: 1_754_000_060 })
  })

  it('目标成交尚未加载时不把视窗推到数据边缘', async () => {
    const start = 1_754_000_000
    const candles = Array.from({ length: 61 }, (_, index) => ({
      time: start + index,
      open: 1,
      high: 1.2,
      low: 0.9,
      close: 1.1,
      volume: 10,
    }))
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles,
        trade: {
          id: 't-4',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: (start + 30) * 1000,
          entry_price: 1.1,
          exit_time: (start + 600) * 1000,
          net_pnl: 1,
        },
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    const rangeCalls = setVisibleLogicalRange.mock.calls.length
    ;(wrapper.vm as unknown as { focusExit: () => void }).focusExit()
    expect(setVisibleLogicalRange).toHaveBeenCalledTimes(rangeCalls)
  })

  it('1s十字光标使用完整时间并逐行展示K线和指标值', async () => {
    const start = 1_754_000_000
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles: [
          { time: start, open: 1, high: 1.2, low: 0.9, close: 1.1, volume: 12345 },
          { time: start + 1, open: 1.1, high: 1.3, low: 1, close: 1.2, volume: 15000 },
        ],
        trade: {
          id: 't-5',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: start * 1000,
          entry_price: 1.1,
          net_pnl: 1,
        },
        indicators: { volume: true, macd: true, ema: true, kdj: true },
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    const options = vi.mocked(createChart).mock.calls.at(-1)?.[1] as {
      timeScale: { secondsVisible: boolean }
      localization: { timeFormatter: (time: number) => string }
    }
    expect(options.timeScale.secondsVisible).toBe(true)
    expect(options.localization.timeFormatter(start)).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/)

    const data = new Map<unknown, unknown>()
    data.set(seriesApis[0], { open: 1, high: 1.2, low: 0.9, close: 1.1 })
    seriesApis.slice(1).forEach((api, index) => data.set(api, { value: index + 1 }))
    subscribeCrosshairMove.mock.calls.at(-1)?.[0]({ point: { x: 100, y: 100 }, time: start, seriesData: data })
    await wrapper.vm.$nextTick()
    const hoverText = wrapper.get('.chart-hover-label').text()
    expect(hoverText).toContain('时间')
    expect(hoverText).toContain('开')
    expect(hoverText).toContain('高')
    expect(hoverText).toContain('低')
    expect(hoverText).toContain('收')
    expect(hoverText).toContain('成交量')
    expect(wrapper.findAll('.indicator-hover-label')).toHaveLength(4)
  })

  it('按主副图组织全部指标，并在左上角显示带名称的当前值', async () => {
    const settings = cloneChartIndicatorSettings(DEFAULT_CHART_INDICATOR_SETTINGS)
    Object.values(settings.main).forEach((indicator) => {
      indicator.enabled = true
    })
    Object.values(settings.sub).forEach((indicator) => {
      indicator.enabled = true
    })
    const start = 1_754_000_000
    const candles = Array.from({ length: 40 }, (_, index) => ({
      time: start + index * 60,
      open: 10 + index * 0.1,
      high: 10.5 + index * 0.1,
      low: 9.5 + index * 0.1,
      close: 10.2 + index * 0.1,
      volume: 1_000 + index * 20,
    }))

    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles,
        trade: {
          id: 't-all-indicators',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: (start + 20 * 60) * 1000,
          entry_price: 12,
          net_pnl: 1,
        },
        indicatorSettings: settings,
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))

    const paneLabels = wrapper.findAll('.indicator-hover-label')
    expect(paneLabels).toHaveLength(6)
    expect(paneLabels[0].findAll('.indicator-value-line')).toHaveLength(3)
    const indicatorNames = [
      'EMA(9)',
      'MA(5)',
      'BOLL UP',
      'MID',
      'DOWN',
      'VOL',
      'MA(5)',
      'MA(20)',
      'MACD DIF',
      'DEA',
      'HIST',
      'KDJ K',
      'RSI(6)',
      'ATR(14)',
    ]
    const paneText = paneLabels.map((label) => label.text()).join(' ')
    indicatorNames.forEach((name) => expect(paneText).toContain(name))
    expect(paneLabels[0].text()).not.toMatch(/开|高|低|收/)
    expect(paneLabels[0].text()).toMatch(/EMA\(9\) -?\d/)
    expect(paneLabels[0].text()).toMatch(/MA\(5\) -?\d/)
    expect(paneLabels[0].text()).toMatch(/BOLL UP -?\d.+MID -?\d.+DOWN -?\d/)
    expect(paneLabels[1].text()).toMatch(/VOL \d.+MA\(5\) \d.+MA\(20\) \d/)
    expect(paneLabels[2].text()).toMatch(/MACD DIF -?\d.+DEA -?\d.+HIST -?\d/)
    expect(paneLabels[3].text()).toMatch(/KDJ K -?\d.+D -?\d.+J -?\d/)
    expect(paneLabels[4].text()).toMatch(/RSI\(6\) -?\d/)
    expect(paneLabels[5].text()).toMatch(/ATR\(14\) -?\d/)
    expect(paneLabels[0].text()).toMatch(/\d/)
    expect(getComputedStyle(paneLabels[0].element).zIndex).toBe('6')
    expect(seriesOptions.filter((options) => options.lastValueVisible === false).length).toBe(seriesOptions.length)
    expect(seriesOptions.filter((options) => options.priceLineVisible === false).length).toBe(seriesOptions.length)
    expect(seriesOptions.every((options) => options.title === undefined)).toBe(true)

    const guideTitles = createPriceLine.mock.calls.map(([line]) => (line as { title?: string }).title)
    expect(guideTitles).not.toContain('0轴')
    expect(guideTitles).not.toContain('均量5')
    expect(guideTitles).not.toContain('50')
  })

  it('无十字光标时按可见范围最右侧K线刷新OHLC和指标值', async () => {
    const settings = cloneChartIndicatorSettings(DEFAULT_CHART_INDICATOR_SETTINGS)
    Object.values(settings.main).forEach((indicator) => {
      indicator.enabled = false
    })
    Object.values(settings.sub).forEach((indicator) => {
      indicator.enabled = false
    })
    settings.main.ma.enabled = true
    settings.main.ma.lines = [{ period: 2, color: '#f59e0b' }]
    const start = 1_754_000_000
    const candles = Array.from({ length: 6 }, (_, index) => {
      const price = (index + 1) * 10
      return {
        time: start + index * 60,
        open: price,
        high: price + 1,
        low: price - 1,
        close: price,
        volume: 100 + index,
      }
    })
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles,
        trade: {
          id: 't-visible-indicator',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: (start + 3 * 60) * 1000,
          entry_price: 40,
          net_pnl: 1,
        },
        indicatorSettings: settings,
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))

    let lines = wrapper.findAll('.indicator-value-line')
    expect(lines).toHaveLength(1)
    expect(lines[0].text()).toBe('MA(2) 55.00')

    const rangeHandler = subscribeVisibleLogicalRangeChange.mock.calls.at(-1)?.[0] as (range: {
      from: number
      to: number
    }) => void
    visibleLogicalRange = { from: 1.2, to: 3.8 }
    rangeHandler(visibleLogicalRange)
    await wrapper.vm.$nextTick()

    lines = wrapper.findAll('.indicator-value-line')
    expect(lines).toHaveLength(1)
    expect(lines[0].text()).toBe('MA(2) 35.00')

    visibleLogicalRange = { from: 0, to: 0.8 }
    rangeHandler(visibleLogicalRange)
    await wrapper.vm.$nextTick()
    lines = wrapper.findAll('.indicator-value-line')
    expect(lines).toHaveLength(0)
    expect(wrapper.text()).not.toContain('15.00')

    visibleLogicalRange = { from: 0.5, to: 0.8 }
    rangeHandler(visibleLogicalRange)
    await wrapper.vm.$nextTick()
    expect(wrapper.findAll('.indicator-hover-label')).toHaveLength(0)
  })

  it('低价币价格轴、价格线和EMA保留行情实际精度', async () => {
    mount(TradeCandlestickChart, {
      props: {
        candles: [
          { time: 1_754_000_000, open: 0.00000712, high: 0.00000719, low: 0.00000708, close: 0.00000716, volume: 10 },
          { time: 1_754_000_001, open: 0.00000716, high: 0.00000721, low: 0.00000711, close: 0.00000718, volume: 12 },
        ],
        trade: {
          id: 't-low',
          symbol: 'LOWUSDT',
          strategy_id: 'spike-short',
          signal_time: 1_754_000_000_000,
          signal_price: 0.00000712,
          entry_time: 1_754_000_001_000,
          entry_price: 0.00000718,
          invalid_price: 0.00000745,
          tier_prices: [0.00000718, 0.00000726, 0.00000734],
          net_pnl: 1,
        },
        indicators: { ema: true, macd: true },
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    const expected = { type: 'price', precision: 8, minMove: 0.00000001 }
    seriesOptions.slice(0, 6).forEach((options) => expect(options.priceFormat).toEqual(expected))
  })

  it('切换标线显隐时就地增删价格线，不重建图表', async () => {
    const start = 1_754_000_000
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles: [
          { time: start, open: 1, high: 1.3, low: 0.9, close: 1.1, volume: 10 },
          { time: start + 1, open: 1.1, high: 1.2, low: 0.8, close: 1, volume: 12 },
        ],
        trade: {
          id: 't-toggle',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          side: 'SHORT',
          entry_time: start * 1000,
          entry_price: 1.1,
          invalid_price: 1.4,
          tier_prices: [1.1, 1.2, 1.3],
          net_pnl: 1,
        },
        lineVisibility: { tiers: true, average: true, invalid: true, signal: true, extensions: true },
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    const removeCount = remove.mock.calls.length
    const linesAfterMount = createPriceLine.mock.calls.length
    expect(linesAfterMount).toBeGreaterThan(0)

    await wrapper.setProps({
      lineVisibility: { tiers: false, average: true, invalid: true, signal: true, extensions: true },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))

    // 图表没有被销毁重建，只是把旧价格线摘掉重画。
    expect(remove).toHaveBeenCalledTimes(removeCount)
    expect(removePriceLine).toHaveBeenCalledTimes(linesAfterMount)
    const redrawn = createPriceLine.mock.calls.slice(linesAfterMount)
    expect(redrawn.some(([line]) => /^限卖|^卖/.test((line as { title: string }).title))).toBe(false)
  })

  it('切换指标时重建窗格但保留当前视窗', async () => {
    const start = 1_754_000_000
    const candles = Array.from({ length: 80 }, (_, index) => ({
      time: start + index,
      open: 1,
      high: 1.2,
      low: 0.9,
      close: 1.1,
      volume: 10,
    }))
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles,
        trade: {
          id: 't-ind',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: (start + 40) * 1000,
          entry_price: 1.1,
          net_pnl: 1,
        },
        indicators: { volume: false },
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    setVisibleRange.mockClear()

    await wrapper.setProps({ indicators: { volume: true } })
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(setVisibleRange).toHaveBeenCalledWith({ from: 1_754_000_030, to: 1_754_000_060 })
  })

  it('服务端替换指标配置及嵌套周期变化都会重建图表', async () => {
    const start = 1_754_000_000
    const settings = cloneChartIndicatorSettings(DEFAULT_CHART_INDICATOR_SETTINGS)
    Object.values(settings.main).forEach((indicator) => {
      indicator.enabled = false
    })
    Object.values(settings.sub).forEach((indicator) => {
      indicator.enabled = false
    })
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles: Array.from({ length: 40 }, (_, index) => ({
          time: start + index,
          open: 1,
          high: 1.2,
          low: 0.9,
          close: 1.1,
          volume: 10,
        })),
        trade: {
          id: 't-settings-replace',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: (start + 20) * 1000,
          entry_price: 1.1,
          net_pnl: 1,
        },
        indicatorSettings: settings,
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    const initialCharts = vi.mocked(createChart).mock.calls.length

    const enabledSettings = cloneChartIndicatorSettings(settings)
    enabledSettings.main.ma.enabled = true
    await wrapper.setProps({ indicatorSettings: enabledSettings })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(vi.mocked(createChart).mock.calls.length).toBe(initialCharts + 1)
    expect(seriesOptions.at(-1)?.title).toBeUndefined()

    const changedSettings = cloneChartIndicatorSettings(enabledSettings)
    changedSettings.main.ma.lines[0].period = 7
    await wrapper.setProps({ indicatorSettings: changedSettings })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(vi.mocked(createChart).mock.calls.length).toBe(initialCharts + 2)
    expect(seriesOptions.every((options) => options.title === undefined)).toBe(true)
  })

  it('悬停在指标 warm-up 区间时不显示未来的最新指标值，并限制标签在 pane 内', async () => {
    const start = 1_754_000_000
    const settings = cloneChartIndicatorSettings(DEFAULT_CHART_INDICATOR_SETTINGS)
    settings.main.ema.enabled = true
    settings.sub.volume.enabled = true
    const candles = Array.from({ length: 20 }, (_, index) => ({
      time: start + index,
      open: 1,
      high: 1.2,
      low: 0.9,
      close: 1.1 + index * 0.01,
      volume: 10,
    }))
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles,
        trade: {
          id: 't-indicator-warmup',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: (start + 10) * 1000,
          entry_price: 1.2,
          net_pnl: 1,
        },
        indicatorSettings: settings,
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(
      wrapper
        .findAll('.indicator-hover-label')
        .every((label) => (label.attributes('style') || '').includes('max-height')),
    ).toBe(true)

    const candle = { open: 1, high: 1.2, low: 0.9, close: 1.1 }
    const seriesData = new Map<unknown, unknown>([[seriesApis[0], candle]])
    subscribeCrosshairMove.mock.calls.at(-1)?.[0]({ point: { x: 100, y: 100 }, time: start, seriesData })
    await wrapper.vm.$nextTick()

    expect(wrapper.findAll('.indicator-hover-label')).toHaveLength(0)
  })

  it('恢复并保存整体图高与指标窗格，并可重置为默认尺寸', async () => {
    localStorage.setItem('backtest-replay-indicator-pane-stretch-v1', JSON.stringify({ volume: 1.46 }))
    localStorage.setItem('backtest-replay-chart-height-v1', '520')
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles: [{ time: 1_754_000_000, open: 1, high: 1.2, low: 0.9, close: 1.1, volume: 10 }],
        trade: {
          id: 't-6',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: 1_754_000_000_000,
          entry_price: 1.1,
          net_pnl: 1,
        },
        indicators: { volume: true },
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(wrapper.get('.chart-shell').attributes('style')).toContain('height: 520px')
    expect(paneSetStretchFactor).toHaveBeenCalledWith(1.46)
    await wrapper.get('.candlestick-host').trigger('pointerdown')
    window.dispatchEvent(new Event('pointerup'))
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(JSON.parse(localStorage.getItem('backtest-replay-indicator-pane-stretch-v1') || '{}').volume).toBe(1)

    wrapper
      .get('.chart-height-resizer')
      .element.dispatchEvent(new MouseEvent('pointerdown', { button: 0, clientY: 100, bubbles: true }))
    window.dispatchEvent(new MouseEvent('pointermove', { clientY: 180 }))
    window.dispatchEvent(new Event('pointerup'))
    await wrapper.vm.$nextTick()
    expect(wrapper.get('.chart-shell').attributes('style')).toContain('height: 360px')
    expect(localStorage.getItem('backtest-replay-chart-height-v1')).toBe('360')

    await (wrapper.vm as unknown as { resetSize: () => Promise<void> }).resetSize()
    await wrapper.vm.$nextTick()
    expect(wrapper.get('.chart-shell').attributes('style')).toBeUndefined()
    expect(localStorage.getItem('backtest-replay-chart-height-v1')).toBeNull()
    expect(localStorage.getItem('backtest-replay-indicator-pane-stretch-v1')).toBeNull()
  })

  it('同一 tick 内多个 prop 同时变化只重建一次，不留下回收不掉的旧图表', async () => {
    const start = 1_754_000_000
    const candles = Array.from({ length: 80 }, (_, index) => ({
      time: start + index,
      open: 1,
      high: 1.2,
      low: 0.9,
      close: 1.1,
      volume: 10,
    }))
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles,
        trade: {
          id: 't-race',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: (start + 40) * 1000,
          entry_price: 1.1,
          net_pnl: 1,
        },
        indicators: { volume: false },
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    const chartsAfterMount = vi.mocked(createChart).mock.calls.length
    const observersAfterMount = observe.mock.calls.length

    // 换交易和换指标落在同一次 setProps 上，两个 watcher 在同一个 tick 里都要求重建。
    // 渲染串行化之前，它们会在 destroy 与 createChart 之间互相插进去，各建一张图挂到
    // 同一个 host 上，而模块级 chart 只留住后一张——前一张连同它的 ResizeObserver
    // 再也回收不掉。
    await wrapper.setProps({
      trade: {
        id: 't-race-2',
        symbol: 'AKEUSDT',
        strategy_id: 'spike-short',
        entry_time: (start + 50) * 1000,
        entry_price: 1.2,
        net_pnl: -1,
      },
      indicators: { volume: true },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(vi.mocked(createChart).mock.calls.length).toBe(chartsAfterMount + 1)
    expect(observe.mock.calls.length).toBe(observersAfterMount + 1)

    // 建了几张就必须销毁几张，卸载后不能有活着的实例残留。
    wrapper.unmount()
    expect(remove.mock.calls.length).toBe(vi.mocked(createChart).mock.calls.length)
  })

  it('卸载时机落在渲染的 await 中间也不会把图表建回来', async () => {
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles: [{ time: 1_754_000_000, open: 1, high: 1.2, low: 0.9, close: 1.1, volume: 10 }],
        trade: {
          id: 't-unmount',
          symbol: 'AKEUSDT',
          strategy_id: 'spike-short',
          entry_time: 1_754_000_000_000,
          entry_price: 1.1,
          net_pnl: 1,
        },
      },
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    const chartsAfterMount = vi.mocked(createChart).mock.calls.length

    // 重建刚排进队列就卸载。这是 disposed 标记要守住的不变量：
    // 队列里那次渲染必须放弃，不能往已经脱离文档的 host 上再挂图和 observer。
    await wrapper.setProps({ indicators: { volume: true } })
    wrapper.unmount()
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(vi.mocked(createChart).mock.calls.length).toBe(chartsAfterMount)
    expect(remove.mock.calls.length).toBe(chartsAfterMount)
  })
})
