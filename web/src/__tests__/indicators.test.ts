import { describe, expect, it } from 'vitest'
import { ema, emaOfClose, kdj, macd } from '@/features/backtests/indicators'

function bars(closes: number[], highs?: number[], lows?: number[]) {
  return closes.map((close, index) => ({
    open: close,
    high: highs?.[index] ?? close,
    low: lows?.[index] ?? close,
    close
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
  })
})
