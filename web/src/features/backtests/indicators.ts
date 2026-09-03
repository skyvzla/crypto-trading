/**
 * 技术指标计算。
 *
 * 全部是纯函数：输入 K 线数组，输出与输入等长的数值序列（不足周期的位置用
 * null 占位，绘图层负责跳过）。放在组件外面是为了能直接单测——指标算错时
 * 图表看起来「有线」，只有断言数值才能发现。
 */

export interface IndicatorBar {
  open: number
  high: number
  low: number
  close: number
}

/**
 * 指数移动平均。以首个样本作为种子（不留 null 前缀），
 * 与图表历史行为保持一致。
 */
export function ema(values: number[], period: number): number[] {
  const multiplier = 2 / (period + 1)
  let previous: number | null = null
  return values.map((value) => {
    previous = previous === null ? value : (value - previous) * multiplier + previous
    return previous
  })
}

export function emaOfClose(bars: IndicatorBar[], period: number): number[] {
  return ema(
    bars.map((bar) => bar.close),
    period,
  )
}

export interface MacdResult {
  dif: number[]
  dea: number[]
  histogram: number[]
}

/** MACD：DIF = EMA(fast) − EMA(slow)，DEA = EMA(DIF, signal)，柱 = DIF − DEA。 */
export function macd(
  bars: IndicatorBar[],
  { fast = 12, slow = 26, signal = 9 }: { fast?: number; slow?: number; signal?: number } = {},
): MacdResult {
  const closes = bars.map((bar) => bar.close)
  const fastLine = ema(closes, fast)
  const slowLine = ema(closes, slow)
  const dif = fastLine.map((value, index) => value - slowLine[index])
  const dea = ema(dif, signal)
  return { dif, dea, histogram: dif.map((value, index) => value - dea[index]) }
}

export interface KdjResult {
  k: number[]
  d: number[]
  j: number[]
}

/**
 * KDJ。RSV 取最近 `period` 根的最高/最低价，K、D 用 1/3 平滑，J = 3K − 2D。
 *
 * 窗口极值用显式循环而不是 `Math.max(...slice)`：避免每根 K 线都分配一个
 * 临时数组，也避开长窗口下参数展开触发栈溢出。
 */
export function kdj(bars: IndicatorBar[], { period = 9 }: { period?: number } = {}): KdjResult {
  const k: number[] = []
  const d: number[] = []
  const j: number[] = []
  let previousK = 50
  let previousD = 50

  for (let index = 0; index < bars.length; index += 1) {
    const start = Math.max(0, index - period + 1)
    let highest = -Infinity
    let lowest = Infinity
    for (let cursor = start; cursor <= index; cursor += 1) {
      if (bars[cursor].high > highest) highest = bars[cursor].high
      if (bars[cursor].low < lowest) lowest = bars[cursor].low
    }
    const range = highest - lowest
    const rsv = range === 0 ? 50 : ((bars[index].close - lowest) / range) * 100
    previousK = (2 * previousK + rsv) / 3
    previousD = (2 * previousD + previousK) / 3
    k.push(previousK)
    d.push(previousD)
    j.push(3 * previousK - 2 * previousD)
  }

  return { k, d, j }
}
