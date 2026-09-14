import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import EquityCurveChart from '@/features/backtests/EquityCurveChart.vue'

const chartMocks = vi.hoisted(() => ({
  remove: vi.fn(),
  setData: vi.fn(),
}))

vi.mock('lightweight-charts', () => ({
  AreaSeries: {},
  ColorType: { Solid: 'solid' },
  createChart: vi.fn(() => ({
    addSeries: () => ({ setData: chartMocks.setData }),
    applyOptions: vi.fn(),
    remove: chartMocks.remove,
    subscribeCrosshairMove: vi.fn(),
    timeScale: () => ({ fitContent: vi.fn() }),
  })),
}))

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      disconnect() {}
    },
  )
})

describe('EquityCurveChart', () => {
  it('父组件替换收益点数组时重绘，并在同一秒只保留最后一点', async () => {
    const wrapper = mount(EquityCurveChart, {
      props: { points: [{ time: 1_000, value: 10 }] },
    })

    await wrapper.setProps({
      points: [
        { time: 2_000, value: 11 },
        { time: 2_500, value: 12 },
      ],
    })
    await flushPromises()

    expect(chartMocks.setData).toHaveBeenLastCalledWith([{ time: 2, value: 12 }])
    expect(chartMocks.remove).toHaveBeenCalledOnce()
  })
})
