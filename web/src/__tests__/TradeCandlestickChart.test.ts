import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import TradeCandlestickChart from '@/features/backtests/TradeCandlestickChart.vue'

const remove = vi.fn()
const setData = vi.fn()
const createPriceLine = vi.fn()
const observe = vi.fn()
const disconnect = vi.fn()

vi.mock('lightweight-charts', () => ({
  CandlestickSeries: {},
  ColorType: { Solid: 'solid' },
  CrosshairMode: { Normal: 0 },
  createSeriesMarkers: vi.fn(),
  createChart: vi.fn(() => ({
    addSeries: () => ({ setData, createPriceLine }),
    timeScale: () => ({ fitContent: vi.fn(), setVisibleLogicalRange: vi.fn(), subscribeVisibleLogicalRangeChange: vi.fn(), unsubscribeVisibleLogicalRangeChange: vi.fn() }),
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
})
