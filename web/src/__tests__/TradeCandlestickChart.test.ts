import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createChart } from 'lightweight-charts'
import TradeCandlestickChart from '@/features/backtests/TradeCandlestickChart.vue'

const remove = vi.fn()
const setData = vi.fn()
const createPriceLine = vi.fn()
const setVisibleLogicalRange = vi.fn()
const setVisibleRange = vi.fn()
const createSeriesMarkers = vi.fn()
const observe = vi.fn()
const disconnect = vi.fn()
const paneSetStretchFactor = vi.fn()
const subscribeCrosshairMove = vi.fn()
const seriesApis: Array<{ setData: typeof setData; createPriceLine: typeof createPriceLine }> = []
const seriesOptions: Array<Record<string, unknown>> = []
const paneMocks = Array.from({ length: 4 }, (_, index) => ({
  getHeight: vi.fn(() => index === 0 ? 300 : 100),
  getStretchFactor: vi.fn(() => index === 0 ? 3 : 1),
  setStretchFactor: paneSetStretchFactor,
  getHTMLElement: vi.fn(() => ({ getBoundingClientRect: () => ({ top: index * 100 }) }))
}))

vi.mock('lightweight-charts', () => ({
  CandlestickSeries: {},
  HistogramSeries: {},
  LineSeries: {},
  ColorType: { Solid: 'solid' },
  CrosshairMode: { Normal: 0 },
  LineStyle: { Solid: 0, Dashed: 2, Dotted: 1 },
  createSeriesMarkers: (...args: unknown[]) => createSeriesMarkers(...args),
  createChart: vi.fn(() => ({
    addSeries: (_definition: unknown, options: Record<string, unknown> = {}) => {
      const api = { setData, createPriceLine }
      seriesApis.push(api)
      seriesOptions.push(options)
      return api
    },
    timeScale: () => ({ fitContent: vi.fn(), getVisibleRange: vi.fn(() => ({ from: 1_754_000_030, to: 1_754_000_060 })), setVisibleRange, setVisibleLogicalRange, subscribeVisibleLogicalRangeChange: vi.fn(), unsubscribeVisibleLogicalRangeChange: vi.fn() }),
    priceScale: () => ({ applyOptions: vi.fn() }),
    panes: () => paneMocks,
    subscribeCrosshairMove: (handler: unknown) => subscribeCrosshairMove(handler),
    unsubscribeCrosshairMove: vi.fn(),
    applyOptions: vi.fn(),
    remove
  }))
}))

beforeEach(() => {
  vi.clearAllMocks()
  seriesApis.length = 0
  seriesOptions.length = 0
  localStorage.clear()
  vi.stubGlobal('ResizeObserver', class {
    observe = observe
    disconnect = disconnect
  })
})

describe('TradeCandlestickChart', () => {
  it('绘制K线、关键价位，并在卸载时释放图表和观察器', async () => {
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles: [{ time: 1_754_000_000, open: 1, high: 1.2, low: 0.9, close: 1.1, volume: 10 }],
        trade: {
          id: 't-1', symbol: 'AKEUSDT', strategy_id: 'spike-short', entry_time: 1_754_000_000_000,
          entry_price: 1.1, average_entry_price: 1.1, invalid_price: 1.3, exit_time: 1_754_000_060_000,
          exit_price: 1.2, net_pnl: -10, tier_prices: [1.1, 1.15, 1.2]
        }
      }
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(setData).toHaveBeenCalledOnce()
    expect(createPriceLine).toHaveBeenCalledTimes(5)
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
      volume: 10
    }))
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles,
        trade: {
          id: 't-2', symbol: 'AKEUSDT', strategy_id: 'spike-short', entry_time: (start + 40) * 1000,
          entry_price: 1.1, exit_time: (start + 80) * 1000, exit_price: 1.2, net_pnl: -10,
          fills: [{ id: 'f-1', time: (start + 40) * 1000, price: 1.1, tier: 1 }]
        }
      }
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(setVisibleLogicalRange).toHaveBeenCalledWith({ from: 10, to: 70 })
    ;(wrapper.vm as unknown as { focusExit: () => void }).focusExit()
    expect(setVisibleLogicalRange).toHaveBeenLastCalledWith({ from: 50, to: 100 })
    expect(createSeriesMarkers).toHaveBeenCalled()
  })

  it('追加K线时更新现有series并保留缩放和可视位置', async () => {
    const start = 1_754_000_000
    const candles = Array.from({ length: 80 }, (_, index) => ({ time: start + index, open: 1, high: 1.2, low: 0.9, close: 1.1, volume: 10 }))
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles,
        trade: { id: 't-3', symbol: 'AKEUSDT', strategy_id: 'spike-short', entry_time: (start + 40) * 1000, entry_price: 1.1, net_pnl: 1 }
      }
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    const removeCount = remove.mock.calls.length
    await wrapper.setProps({ candles: [...candles, { time: start + 80, open: 1.1, high: 1.3, low: 1, close: 1.2, volume: 12 }] })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(remove).toHaveBeenCalledTimes(removeCount)
    expect(setData).toHaveBeenLastCalledWith(expect.arrayContaining([expect.objectContaining({ time: start + 80 })]))
    expect(setVisibleRange).toHaveBeenCalledWith({ from: 1_754_000_030, to: 1_754_000_060 })
  })

  it('目标成交尚未加载时不把视窗推到数据边缘', async () => {
    const start = 1_754_000_000
    const candles = Array.from({ length: 61 }, (_, index) => ({ time: start + index, open: 1, high: 1.2, low: 0.9, close: 1.1, volume: 10 }))
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles,
        trade: { id: 't-4', symbol: 'AKEUSDT', strategy_id: 'spike-short', entry_time: (start + 30) * 1000, entry_price: 1.1, exit_time: (start + 600) * 1000, net_pnl: 1 }
      }
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
          { time: start + 1, open: 1.1, high: 1.3, low: 1, close: 1.2, volume: 15000 }
        ],
        trade: { id: 't-5', symbol: 'AKEUSDT', strategy_id: 'spike-short', entry_time: start * 1000, entry_price: 1.1, net_pnl: 1 },
        indicators: { volume: true, macd: true, ema: true, kdj: true }
      }
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    const options = vi.mocked(createChart).mock.calls.at(-1)?.[1] as { timeScale: { secondsVisible: boolean }; localization: { timeFormatter: (time: number) => string } }
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

  it('低价币价格轴、价格线和EMA保留行情实际精度', async () => {
    mount(TradeCandlestickChart, {
      props: {
        candles: [
          { time: 1_754_000_000, open: 0.00000712, high: 0.00000719, low: 0.00000708, close: 0.00000716, volume: 10 },
          { time: 1_754_000_001, open: 0.00000716, high: 0.00000721, low: 0.00000711, close: 0.00000718, volume: 12 }
        ],
        trade: {
          id: 't-low', symbol: 'LOWUSDT', strategy_id: 'spike-short', signal_time: 1_754_000_000_000,
          signal_price: 0.00000712, entry_time: 1_754_000_001_000, entry_price: 0.00000718,
          invalid_price: 0.00000745, tier_prices: [0.00000718, 0.00000726, 0.00000734], net_pnl: 1
        },
        indicators: { ema: true, macd: true }
      }
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    const expected = { type: 'price', precision: 8, minMove: 0.00000001 }
    seriesOptions.slice(0, 6).forEach((options) => expect(options.priceFormat).toEqual(expected))
  })

  it('恢复并保存整体图高与指标窗格，并可重置为默认尺寸', async () => {
    localStorage.setItem('backtest-replay-indicator-pane-stretch-v1', JSON.stringify({ volume: 1.46 }))
    localStorage.setItem('backtest-replay-chart-height-v1', '520')
    const wrapper = mount(TradeCandlestickChart, {
      props: {
        candles: [{ time: 1_754_000_000, open: 1, high: 1.2, low: 0.9, close: 1.1, volume: 10 }],
        trade: { id: 't-6', symbol: 'AKEUSDT', strategy_id: 'spike-short', entry_time: 1_754_000_000_000, entry_price: 1.1, net_pnl: 1 },
        indicators: { volume: true }
      }
    })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(wrapper.get('.chart-shell').attributes('style')).toContain('height: 520px')
    expect(paneSetStretchFactor).toHaveBeenCalledWith(1.46)
    await wrapper.get('.candlestick-host').trigger('pointerdown')
    window.dispatchEvent(new Event('pointerup'))
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(JSON.parse(localStorage.getItem('backtest-replay-indicator-pane-stretch-v1') || '{}').volume).toBe(1)

    wrapper.get('.chart-height-resizer').element.dispatchEvent(new MouseEvent('pointerdown', { button: 0, clientY: 100, bubbles: true }))
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
})
