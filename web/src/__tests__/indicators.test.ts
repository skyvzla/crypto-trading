import { describe, expect, it } from 'vitest'
import {
  atr,
  bollinger,
  ema,
  emaOfClose,
  kdj,
  ma,
  macd,
  rsi,
  sma,
  smaOfClose,
  volumeSma,
} from '@/features/backtests/indicators'

function bars(closes: number[], highs?: number[], lows?: number[], volumes?: number[]) {
  return closes.map((close, index) => ({
    open: close,
    high: highs?.[index] ?? close,
    low: lows?.[index] ?? close,
    close,
    volume: volumes?.[index],
  }))
}

describe('技术指标', () => {
  it('EMA 以首个样本为种子并按 2/(n+1) 收敛', () => {
    // period 1 时权重为 1，EMA 退化成原序列。
    expect(ema([1, 2, 3], 1)).toEqual([1, 2, 3])
    // period 3 → multiplier 0.5：10, 10+(20-10)*.5=15, 15+(30-15)*.5=22.5
    expect(ema([10, 20, 30], 3)).toEqual([10, 15, 22.5])
  })

  it('EMA 输出与输入等长，便于按下标对齐 K 线', () => {
    const closes = Array.from({ length: 50 }, (_, index) => index + 1)
    expect(emaOfClose(bars(closes), 21)).toHaveLength(50)
  })

  it('SMA/MA 在完整窗口后输出滚动算术平均值，前置位置用 null 对齐', () => {
    const closes = [10, 20, 30, 40]

    expect(sma(closes, 3)).toEqual([null, null, 20, 30])
    expect(ma(closes, 3)).toEqual([null, null, 20, 30])
    expect(smaOfClose(bars(closes), 2)).toEqual([null, 15, 25, 35])
  })

  it('成交量均线使用 volume 字段并保留 K 线下标对齐', () => {
    expect(volumeSma(bars([10, 11, 12], undefined, undefined, [100, 200, 300]), 2)).toEqual([null, 150, 250])
    // 没有成交量时不能把缺失值当成 0，否则均线会被静默压低。
    expect(volumeSma(bars([10, 11, 12]), 2)).toEqual([null, null, null])
  })

  it('BOLL 使用 SMA 中轨和窗口总体标准差计算上下轨', () => {
    const result = bollinger([1, 2, 3, 4], { period: 3, multiplier: 2 })
    const deviation = Math.sqrt(2 / 3)

    expect(result.middle).toEqual([null, null, 2, 3])
    expect(result.upper[2]).toBeCloseTo(2 + deviation * 2, 10)
    expect(result.lower[2]).toBeCloseTo(2 - deviation * 2, 10)
    expect(result.upper[3]).toBeCloseTo(3 + deviation * 2, 10)
    expect(result.lower[3]).toBeCloseTo(3 - deviation * 2, 10)
  })

  it('BOLL period=1 时标准差为 0，上下轨都等于中轨', () => {
    expect(bollinger([3, 5], 1)).toEqual({
      middle: [3, 5],
      upper: [3, 5],
      lower: [3, 5],
    })
  })

  it('RSI 使用 Wilder 初始平均与递推平均，并在 period 根变化后首次输出', () => {
    const result = rsi([1, 2, 3, 2, 2, 4], { period: 3 })

    // 前三次变化为 +1、+1、-1：平均涨幅 2/3、平均跌幅 1/3，RSI=66.666...。
    expect(result.slice(0, 3)).toEqual([null, null, null])
    expect(result[3]).toBeCloseTo(66.6666666667, 10)
    // 后续按 Wilder: avg=(previous*(n-1)+current)/n。
    expect(result[4]).toBeCloseTo(66.6666666667, 10)
    expect(result[5]).toBeCloseTo(86.6666666667, 10)
  })

  it('RSI 对无涨跌、全涨和全跌分别处理无除数情况', () => {
    expect(rsi([5, 5, 5, 5], 2)).toEqual([null, null, 50, 50])
    expect(rsi([1, 2, 3, 4], 2)).toEqual([null, null, 100, 100])
    expect(rsi([4, 3, 2, 1], 2)).toEqual([null, null, 0, 0])
  })

  it('ATR 先计算 True Range，再用 Wilder 平滑，首个完整窗口输出在 period-1', () => {
    const result = atr(bars([11, 14, 15, 11], [12, 15, 16, 14], [10, 11, 13, 10]), { period: 3 })

    // TR=[2,4,3,5]，初始 ATR=(2+4+3)/3=3；下一根为 (3*2+5)/3=11/3。
    expect(result.slice(0, 2)).toEqual([null, null])
    expect(result[2]).toBeCloseTo(3, 10)
    expect(result[3]).toBeCloseTo(11 / 3, 10)
  })

  it('MACD 满足 DIF=EMAfast-EMAslow、柱=DIF-DEA', () => {
    const closes = [10, 11, 12, 11, 13, 15, 14, 16, 18, 17]
    const result = macd(bars(closes), { fast: 3, slow: 6, signal: 3 })
    const fast = ema(closes, 3)
    const slow = ema(closes, 6)

    expect(result.dif).toHaveLength(closes.length)
    result.dif.forEach((value, index) => expect(value).toBeCloseTo(fast[index] - slow[index], 10))
    expect(result.dea).toEqual(ema(result.dif, 3))
    result.histogram.forEach((value, index) => expect(value).toBeCloseTo(result.dif[index] - result.dea[index], 10))
  })

  it('KDJ 的 RSV 在窗口内无波动时取中值 50，且 J=3K-2D', () => {
    const flat = kdj(bars([5, 5, 5, 5]), { period: 3 })
    // 全平盘时 RSV=50，K 与 D 的初值也是 50，因此整段保持 50。
    flat.k.forEach((value) => expect(value).toBeCloseTo(50, 10))
    flat.d.forEach((value) => expect(value).toBeCloseTo(50, 10))
    flat.j.forEach((value) => expect(value).toBeCloseTo(50, 10))

    const result = kdj(bars([10, 12, 11, 14], [11, 13, 12, 15], [9, 11, 10, 13]), { period: 2 })
    result.j.forEach((value, index) => {
      expect(value).toBeCloseTo(3 * result.k[index] - 2 * result.d[index], 10)
    })
  })

  it('KDJ 收在窗口最高价时 RSV 为 100，K 单调上行', () => {
    const rising = kdj(bars([10, 11, 12, 13, 14], [10, 11, 12, 13, 14], [10, 11, 12, 13, 14]), { period: 2 })
    // 第 0 根的窗口只有它自己，high==low 没有区间，按约定取 RSV=50。
    expect(rising.k[0]).toBeCloseTo(50, 10)
    // 之后每根都收在窗口最高，RSV 恒为 100，K 从 50 逐步逼近 100。
    expect(rising.k[1]).toBeCloseTo((2 * 50 + 100) / 3, 10)
    for (let index = 2; index < rising.k.length; index += 1) {
      expect(rising.k[index]).toBeGreaterThan(rising.k[index - 1])
      expect(rising.k[index]).toBeLessThan(100)
    }
  })

  it('空 K 线序列不抛异常', () => {
    expect(emaOfClose([], 9)).toEqual([])
    expect(macd([]).dif).toEqual([])
    expect(kdj([])).toEqual({ k: [], d: [], j: [] })
    expect(sma([], 9)).toEqual([])
    expect(bollinger([], 9)).toEqual({ middle: [], upper: [], lower: [] })
    expect(rsi([], 9)).toEqual([])
    expect(atr([], 9)).toEqual([])
    expect(volumeSma([], 9)).toEqual([])
  })

  it('非法或边界 period 不产生除零、NaN 或越界结果', () => {
    for (const period of [0, -1, 1.5, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(sma([1, 2, 3], period)).toEqual([null, null, null])
      expect(bollinger([1, 2, 3], period).middle).toEqual([null, null, null])
      expect(rsi([1, 2, 3], period)).toEqual([null, null, null])
      expect(atr(bars([1, 2, 3]), period)).toEqual([null, null, null])
    }

    expect(sma([1, 2, 3], 1)).toEqual([1, 2, 3])
    expect(atr(bars([1, 2, 3]), 1)).toEqual([0, 1, 1])
  })
})
