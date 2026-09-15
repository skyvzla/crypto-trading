import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import EquityCurveChart, { toEquitySeriesPoints } from '@/features/backtests/EquityCurveChart.vue'
import { getChartTheme, type ChartTheme } from '@/features/backtests/chartTheme'

const chartMocks = vi.hoisted(() => ({
  setData: vi.fn(),
  /** 每次 createChart 的 remove 都要单独计数，才能判断是哪一张图被销毁。 */
  removes: [] as Array<ReturnType<typeof vi.fn>>,
  failNextSetData: { value: false },
}))

vi.mock('lightweight-charts', () => ({
  AreaSeries: {},
  ColorType: { Solid: 'solid' },
  createChart: vi.fn(() => {
    const remove = vi.fn()
    chartMocks.removes.push(remove)
    return {
      addSeries: () => ({
        setData: (data: unknown) => {
          if (chartMocks.failNextSetData.value) {
            chartMocks.failNextSetData.value = false
            throw new Error('data must be asc ordered by time')
          }
          chartMocks.setData(data)
        },
      }),
      applyOptions: vi.fn(),
      remove,
      subscribeCrosshairMove: vi.fn(),
      timeScale: () => ({ fitContent: vi.fn() }),
    }
  }),
}))

beforeEach(() => {
  vi.clearAllMocks()
  chartMocks.removes.length = 0
  chartMocks.failNextSetData.value = false
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      disconnect() {}
    },
  )
})

/**
 * WCAG 2.x 相对亮度对比度。数值按 sRGB 通道线性化后加权，不做任何“看起来够深”的目测。
 */
function relativeLuminance(hex: string): number {
  const channels = hex
    .slice(1)
    .match(/.{2}/g)!
    .map((channel) => Number.parseInt(channel, 16) / 255)
    .map((channel) => (channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4))
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
}

function contrastRatio(foreground: string, background: string): number {
  const foregroundLuminance = relativeLuminance(foreground)
  const backgroundLuminance = relativeLuminance(background)
  return (
    (Math.max(foregroundLuminance, backgroundLuminance) + 0.05) /
    (Math.min(foregroundLuminance, backgroundLuminance) + 0.05)
  )
}

/** 带 alpha 的色值先按画布底色合成为实色，再算对比度。 */
function composite(fill: string, background: string): string {
  const alpha = Number.parseInt(fill.slice(7, 9), 16) / 255
  const channels = [1, 3, 5].map((offset) => {
    const foreground = Number.parseInt(fill.slice(offset, offset + 2), 16)
    const backdrop = Number.parseInt(background.slice(offset, offset + 2), 16)
    return Math.round(foreground * alpha + backdrop * (1 - alpha))
  })
  return `#${channels.map((channel) => channel.toString(16).padStart(2, '0')).join('')}`
}

/** 带 alpha 的标记先与画布底色合成再比，避免用半透明值算出虚高的对比度。 */
function ratioOn(color: string, background: string): number {
  return contrastRatio(color.length === 9 ? composite(color, background) : color, background)
}

const LIGHT = getChartTheme(false)
const DARK = getChartTheme(true)

/** 画布上的数据标记：非文字图形，1.4.11 要求 3:1。 */
const LIGHT_MARK_KEYS: Array<keyof ChartTheme> = [
  'paneSeparator',
  'up',
  'down',
  'signal',
  'filled',
  'pending',
  'average',
  'invalid',
  'entryMarker',
  'exitProfit',
  'exitLoss',
  'overlayMarker',
  'overlayLine',
  'areaLine',
]

const LIGHT_INDICATOR_MARK_KEYS = [
  'ema9',
  'ema21',
  'volume',
  'volumeLabel',
  'volumeUp',
  'volumeDown',
  'macdDif',
  'macdDea',
  'macdHistogram',
  'macdHistogramUp',
  'macdHistogramDown',
  'kdjK',
  'kdjD',
  'kdjJ',
] as const

const DARK_MARK_KEYS: Array<keyof ChartTheme> = [
  'paneSeparator',
  'paneSeparatorHover',
  'up',
  'down',
  'signal',
  'filled',
  'pending',
  'average',
  'invalid',
  'entryMarker',
  'exitProfit',
  'exitLoss',
  'overlayMarker',
  'overlayLine',
  'areaLine',
]

describe('图表调色板对比度', () => {
  it('浅色画布上的数据标记都达到 3:1', () => {
    for (const key of LIGHT_MARK_KEYS) {
      const color = LIGHT[key] as string
      expect(ratioOn(color, LIGHT.background), `${key} ${color}`).toBeGreaterThanOrEqual(3)
    }
    for (const key of LIGHT_INDICATOR_MARK_KEYS) {
      const color = LIGHT.indicators[key]
      expect(ratioOn(color, LIGHT.background), `indicators.${key} ${color}`).toBeGreaterThanOrEqual(3)
    }
  })

  it('浅色画布上的文字色达到 4.5:1', () => {
    expect(contrastRatio(LIGHT.text, LIGHT.background)).toBeGreaterThanOrEqual(4.5)
    expect(contrastRatio(LIGHT.axisText, LIGHT.background)).toBeGreaterThanOrEqual(4.5)
  })

  it('深色画布上的数据标记和文字不退化', () => {
    for (const key of DARK_MARK_KEYS) {
      const color = DARK[key] as string
      expect(ratioOn(color, DARK.background), `${key} ${color}`).toBeGreaterThanOrEqual(3)
    }
    for (const key of LIGHT_INDICATOR_MARK_KEYS) {
      const color = DARK.indicators[key]
      expect(ratioOn(color, DARK.background), `indicators.${key} ${color}`).toBeGreaterThanOrEqual(3)
    }
    expect(contrastRatio(DARK.text, DARK.background)).toBeGreaterThanOrEqual(4.5)
    expect(contrastRatio(DARK.axisText, DARK.background)).toBeGreaterThanOrEqual(4.5)
  })

  it('同一语义的绿/红在两个主题里都不低于各自画布的 3:1', () => {
    // 成交量、MACD 柱、退出盈利标记共用一套绿色语义；易位到浅色画布时
    // 必须换用更深的值，照搬深色值会掉到 2.40:1。
    expect(ratioOn(LIGHT.indicators.volume, LIGHT.background)).toBeGreaterThanOrEqual(3)
    expect(ratioOn(LIGHT.exitProfit, LIGHT.background)).toBeGreaterThanOrEqual(3)
    expect(ratioOn(DARK.indicators.volume, DARK.background)).toBeGreaterThanOrEqual(3)
    expect(ratioOn(DARK.exitProfit, DARK.background)).toBeGreaterThanOrEqual(3)
  })
})

describe('toEquitySeriesPoints', () => {
  it('同一秒只保留最后一个点，并升序输出唯一时间', () => {
    const points = toEquitySeriesPoints([
      { time: 2_000, value: 12 },
      { time: 1_000, value: 10 },
      { time: 1_500, value: 11 },
      { time: 3_000, value: 13 },
    ])

    expect(points).toEqual([
      { time: 1, value: 11 },
      { time: 2, value: 12 },
      { time: 3, value: 13 },
    ])
    const times = points.map((point) => point.time)
    expect(times).toEqual([...new Set(times)])
    expect(times).toEqual([...times].sort((left, right) => left - right))
  })

  it('0 秒持仓（entry_time == exit_time）产生的同秒点不会触发图表库的重复时间错误', () => {
    // 两笔都在 1_700_000_000_500 结算，只保留后一笔的权益值。
    const points = toEquitySeriesPoints([
      { time: 1_700_000_000_500, value: 10_000 },
      { time: 1_700_000_000_500, value: 9_800 },
    ])

    expect(points).toEqual([{ time: 1_700_000_000, value: 9_800 }])
  })

  it('空输入返回空序列', () => {
    expect(toEquitySeriesPoints([])).toEqual([])
  })
})

describe('EquityCurveChart 渲染失败', () => {
  it('setData 抛错时保留上一张图，并给出可见提示', async () => {
    const wrapper = mount(EquityCurveChart, {
      props: { points: [{ time: 1_000, value: 10 }] },
    })
    await flushPromises()
    expect(wrapper.findAll('.equity-chart-canvas')).toHaveLength(1)
    expect(chartMocks.setData).toHaveBeenCalledTimes(1)

    chartMocks.failNextSetData.value = true
    await wrapper.setProps({ points: [{ time: 2_000, value: 11 }] })
    await flushPromises()

    // 旧图既没有被销毁，也没有被新容器顶掉。
    expect(chartMocks.removes).toHaveLength(2)
    expect(chartMocks.removes[0]).not.toHaveBeenCalled()
    expect(chartMocks.removes[1]).toHaveBeenCalledOnce()
    expect(wrapper.findAll('.equity-chart-canvas')).toHaveLength(1)
    expect(chartMocks.setData).toHaveBeenCalledTimes(1)
    expect(wrapper.get('[role="alert"]').text()).toContain('权益曲线绘制失败')
  })
})
