import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import TradeCandlestickChart from '@/features/backtests/TradeCandlestickChart.vue'

const remove = vi.fn()
const setData = vi.fn()
const createPriceLine = vi.fn()
const setVisibleLogicalRange = vi.fn()
const setVisibleRange = vi.fn()
const createSeriesMarkers = vi.fn()
const observe = vi.fn()
const disconnect = vi.fn()

vi.mock('lightweight-charts', () => ({
  CandlestickSeries: {},
  HistogramSeries: {},
  LineSeries: {},
  ColorType: { Solid: 'solid' },
  CrosshairMode: { Normal: 0 },
  LineStyle: { Solid: 0, Dashed: 2, Dotted: 1 },
  createSeriesMarkers: (...args: unknown[]) => createSeriesMarkers(...args),
  createChart: vi.fn(() => ({
    addSeries: () => ({ setData, createPriceLine }),
    timeScale: () => ({ fitContent: vi.fn(), getVisibleRange: vi.fn(() => ({ from: 1_754_000_030, to: 1_754_000_060 })), setVisibleRange, setVisibleLogicalRange, subscribeVisibleLogicalRangeChange: vi.fn(), unsubscribeVisibleLogicalRangeChange: vi.fn() }),
    priceScale: () => ({ applyOptions: vi.fn() }),
    subscribeCrosshairMove: vi.fn(),
    unsubscribeCrosshairMove: vi.fn(),
    applyOptions: vi.fn(),
    remove
  }))
}))

beforeEach(() => {
  vi.clearAllMocks()
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
})
