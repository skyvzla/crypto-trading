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
  /** 成交量在部分仅价格的测试/数据源中可能缺失。 */
  volume?: number
}

export type IndicatorValue = number | null
export type IndicatorSeries = IndicatorValue[]

function isValidPeriod(period: number): boolean {
  return Number.isSafeInteger(period) && period >= 1
}

function nullSeries(length: number): IndicatorSeries {
  return Array.from({ length }, () => null)
}

function periodFromOptions(options: number | { period?: number } | undefined, fallback: number): number {
  return typeof options === 'number' ? options : (options?.period ?? fallback)
}

/**
 * 简单移动平均（SMA）。只有窗口内的样本全部为有限数值时才输出结果，
 * 因此前 `period - 1` 根 K 线以及缺失数据影响的窗口均为 null。
 */
export function sma(values: number[], period: number): IndicatorSeries {
  const result = nullSeries(values.length)
  if (!isValidPeriod(period)) return result

  let sum = 0
  let invalidCount = 0
  for (let index = 0; index < values.length; index += 1) {
    const value = values[index]
    if (Number.isFinite(value)) sum += value
    else invalidCount += 1

    const expired = index - period
    if (expired >= 0) {
      const expiredValue = values[expired]
      if (Number.isFinite(expiredValue)) sum -= expiredValue
      else invalidCount -= 1
    }

    if (index >= period - 1 && invalidCount === 0) result[index] = sum / period
  }
  return result
}

/** MA 是 SMA 的常用简称，保留独立导出便于指标配置按名称查找。 */
export const ma = sma

export function smaOfClose(bars: IndicatorBar[], period: number): IndicatorSeries {
  return sma(
    bars.map((bar) => bar.close),
    period,
  )
}

export const maOfClose = smaOfClose

/** 成交量 SMA；也接受纯成交量数组，方便图表层直接复用。 */
export function volumeSma(values: IndicatorBar[] | number[], period: number): IndicatorSeries {
  const volumes = values.map((value) => (typeof value === 'number' ? value : (value.volume ?? Number.NaN)))
  return sma(volumes, period)
}

export const volumeMa = volumeSma
export const smaOfVolume = volumeSma

export interface BollingerOptions {
  period?: number
  multiplier?: number
  /** `stdDev` 是 multiplier 的兼容性别名。 */
  stdDev?: number
  /** `deviation` 与图表配置中的字段名保持兼容。 */
  deviation?: number
}

export interface BollingerResult {
  middle: IndicatorSeries
  upper: IndicatorSeries
  lower: IndicatorSeries
}

/**
 * 布林带：中轨为 SMA，带宽为窗口总体标准差（不是样本标准差），
 * 上下轨分别为 `middle +/- multiplier * standardDeviation`。
 */
export function bollinger(values: number[] | IndicatorBar[], options: number | BollingerOptions = {}): BollingerResult {
  const closes = values.map((value) => (typeof value === 'number' ? value : value.close))
  const settings = typeof options === 'number' ? { period: options } : options
  const period = periodFromOptions(settings, 20)
  const multiplier = settings.multiplier ?? settings.stdDev ?? settings.deviation ?? 2
  const middle = sma(closes, period)
  const upper = nullSeries(closes.length)
  const lower = nullSeries(closes.length)

  if (!isValidPeriod(period) || !Number.isFinite(multiplier) || multiplier < 0) {
    return { middle, upper, lower }
  }

  let sum = 0
  let sumSquares = 0
  let invalidCount = 0
  for (let index = 0; index < closes.length; index += 1) {
    const value = closes[index]
    if (Number.isFinite(value)) {
      sum += value
      sumSquares += value * value
    } else {
      invalidCount += 1
    }

    const expired = index - period
    if (expired >= 0) {
      const expiredValue = closes[expired]
      if (Number.isFinite(expiredValue)) {
        sum -= expiredValue
        sumSquares -= expiredValue * expiredValue
      } else {
        invalidCount -= 1
      }
    }

    if (index < period - 1 || invalidCount !== 0) continue
    const mean = sum / period
    const variance = Math.max(0, sumSquares / period - mean * mean)
    const band = Math.sqrt(variance) * multiplier
    upper[index] = mean + band
    lower[index] = mean - band
  }

  return { middle, upper, lower }
}

export const bollingerBands = bollinger
export const boll = bollinger

/**
 * RSI（Relative Strength Index）。先用前 `period` 个涨跌的算术平均初始化，
 * 后续使用 Wilder 的递推平均；全涨/全跌/无波动分别处理为 100/0/50。
 */
export function rsi(values: number[] | IndicatorBar[], options: number | { period?: number } = {}): IndicatorSeries {
  const closes = values.map((value) => (typeof value === 'number' ? value : value.close))
  const period = periodFromOptions(options, 14)
  const result = nullSeries(closes.length)
  if (!isValidPeriod(period) || closes.length <= period) return result

  let validChanges = 0
  let gainSum = 0
  let lossSum = 0
  let averageGain = 0
  let averageLoss = 0
  for (let index = 1; index < closes.length; index += 1) {
    const change = closes[index] - closes[index - 1]
    if (!Number.isFinite(change)) {
      validChanges = 0
      gainSum = 0
      lossSum = 0
      averageGain = 0
      averageLoss = 0
      continue
    }

    const gain = change > 0 ? change : 0
    const loss = change < 0 ? -change : 0

    if (validChanges < period) {
      validChanges += 1
      gainSum += gain
      lossSum += loss
      if (validChanges < period) continue
      averageGain = gainSum / period
      averageLoss = lossSum / period
    } else {
      averageGain = (averageGain * (period - 1) + gain) / period
      averageLoss = (averageLoss * (period - 1) + loss) / period
    }
    result[index] = relativeStrength(averageGain, averageLoss)
  }

  return result
}

function relativeStrength(averageGain: number, averageLoss: number): number {
  if (averageLoss === 0) return averageGain === 0 ? 50 : 100
  if (averageGain === 0) return 0
  return 100 - 100 / (1 + averageGain / averageLoss)
}

export const rsiOfClose = rsi

/**
 * ATR（Average True Range）。第一根 K 线的 TR 取 high-low，
 * 初始 ATR 是前 `period` 个 TR 的算术平均，之后按 Wilder 方法平滑。
 */
export function atr(bars: IndicatorBar[], options: number | { period?: number } = {}): IndicatorSeries {
  const period = periodFromOptions(options, 14)
  const result = nullSeries(bars.length)
  if (!isValidPeriod(period)) return result

  const trueRanges: Array<number | null> = bars.map((bar, index) => {
    if (!Number.isFinite(bar.high) || !Number.isFinite(bar.low) || !Number.isFinite(bar.close)) return null
    if (index === 0) return bar.high - bar.low
    const previousClose = bars[index - 1].close
    if (!Number.isFinite(previousClose)) return null
    return Math.max(bar.high - bar.low, Math.abs(bar.high - previousClose), Math.abs(bar.low - previousClose))
  })

  let validCount = 0
  let sum = 0
  let average: number | null = null
  for (let index = 0; index < trueRanges.length; index += 1) {
    const trueRange = trueRanges[index]
    if (trueRange === null) {
      validCount = 0
      sum = 0
      average = null
      continue
    }

    if (average === null) {
      validCount += 1
      sum += trueRange
      if (validCount === period) {
        average = sum / period
        result[index] = average
      }
      continue
    }

    average = (average * (period - 1) + trueRange) / period
    result[index] = average
  }

  return result
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
