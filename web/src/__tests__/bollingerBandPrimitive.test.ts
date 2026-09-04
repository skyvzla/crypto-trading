import { describe, expect, it, vi } from 'vitest'
import { BollingerBandPrimitive, colorWithOpacity } from '@/features/backtests/bollingerBandPrimitive'

describe('BollingerBandPrimitive', () => {
  it('在上下轨坐标之间绘制背景填充并请求刷新', () => {
    const requestUpdate = vi.fn()
    const primitive = new BollingerBandPrimitive({ fillColor: '#eab3081f' })
    primitive.attached({
      chart: {
        timeScale: () => ({ timeToCoordinate: (time: number) => time - 100 }),
      },
      series: {
        priceToCoordinate: (price: number) => price * 10,
      },
      requestUpdate,
      horzScaleBehavior: {},
    } as never)
    primitive.setData([
      { time: 100 as never, upper: 12, lower: 8 },
      { time: 160 as never, upper: 13, lower: 9 },
    ])

    const context = {
      fillStyle: '',
      save: vi.fn(),
      restore: vi.fn(),
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      closePath: vi.fn(),
      fill: vi.fn(),
    }
    const renderer = primitive.paneViews()[0].renderer()
    renderer?.drawBackground?.({
      useMediaCoordinateSpace: (draw: (scope: { context: typeof context }) => void) => draw({ context }),
    } as never)

    expect(requestUpdate).toHaveBeenCalledOnce()
    expect(primitive.paneViews()[0].zOrder?.()).toBe('bottom')
    expect(context.fillStyle).toBe('#eab3081f')
    expect(context.moveTo).toHaveBeenCalledWith(0, 120)
    expect(context.lineTo).toHaveBeenNthCalledWith(1, 60, 130)
    expect(context.lineTo).toHaveBeenNthCalledWith(2, 60, 90)
    expect(context.lineTo).toHaveBeenNthCalledWith(3, 0, 80)
    expect(context.fill).toHaveBeenCalledOnce()
  })

  it('颜色透明度覆盖已有 alpha 并限制在有效范围', () => {
    expect(colorWithOpacity('#abcdef80', 0.42)).toBe('#abcdef6b')
    expect(colorWithOpacity('#abcdef', -1)).toBe('#abcdef00')
    expect(colorWithOpacity('#abcdef', 2)).toBe('#abcdefff')
  })

  it('遇到无效数据或坐标空洞时分段绘制，不跨越缺口填充', () => {
    const primitive = new BollingerBandPrimitive({ fillColor: '#eab3081f' })
    primitive.attached({
      chart: {
        timeScale: () => ({ timeToCoordinate: (time: number) => (time === 300 ? null : time) }),
      },
      series: {
        priceToCoordinate: (price: number) => price,
      },
      requestUpdate: vi.fn(),
      horzScaleBehavior: {},
    } as never)
    primitive.setData([
      { time: 100 as never, upper: 12, lower: 8 },
      { time: 200 as never, upper: 13, lower: 9 },
      { time: 300 as never, upper: 14, lower: 10 },
      { time: 400 as never, upper: 15, lower: 11 },
      { time: 500 as never, upper: 16, lower: 12 },
      null,
      { time: 600 as never, upper: Number.NaN, lower: 13 },
      { time: 700 as never, upper: 17, lower: 13 },
      { time: 800 as never, upper: 18, lower: 14 },
    ])

    const context = {
      fillStyle: '',
      save: vi.fn(),
      restore: vi.fn(),
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      closePath: vi.fn(),
      fill: vi.fn(),
    }
    primitive
      .paneViews()[0]
      .renderer()
      ?.drawBackground?.({
        useMediaCoordinateSpace: (draw: (scope: { context: typeof context }) => void) => draw({ context }),
      } as never)

    expect(context.beginPath).toHaveBeenCalledTimes(3)
    expect(context.moveTo).toHaveBeenNthCalledWith(1, 100, 12)
    expect(context.moveTo).toHaveBeenNthCalledWith(2, 400, 15)
    expect(context.moveTo).toHaveBeenNthCalledWith(3, 700, 17)
    expect(context.fill).toHaveBeenCalledTimes(3)
  })
})
