import type { BacktestCandle, BacktestFill, BacktestOrder, JsonObject } from '@/api/types'

export type TradeChartFillTimeSemantics = 'backtest-confirmation' | 'exchange'

export interface TradeChartData {
  id?: string
  strategy_id?: string
  symbol: string
  side?: string | null
  signal_time?: string | number | null
  signal_price?: number | null
  entry_time: string | number
  entry_price: number
  average_entry_price?: number | null
  invalid_price?: number | null
  exit_time?: string | number | null
  exit_price?: number | null
  net_pnl?: number | null
  orders?: BacktestOrder[]
  fills?: BacktestFill[]
  tier_prices?: number[]
  attributes?: JsonObject
  strategy_data?: JsonObject
  metrics?: JsonObject
  parameters?: JsonObject
}

export const MAX_LOADED_CANDLES = 20_000

/** 价格轴精度取行情实际小数位，使用归约避免向 Math.max 展开超大参数列表。 */
export function chartPricePrecision(data: BacktestCandle[]): number {
  const decimalPlaces = (value: number) => {
    const fixed = Math.abs(value).toFixed(12).replace(/0+$/, '')
    const separator = fixed.indexOf('.')
    return separator === -1 ? 0 : fixed.length - separator - 1
  }
  const maximum = data.reduce(
    (current, bar) =>
      Math.max(
        current,
        decimalPlaces(bar.open),
        decimalPlaces(bar.high),
        decimalPlaces(bar.low),
        decimalPlaces(bar.close),
      ),
    0,
  )
  return Math.min(12, Math.max(2, maximum))
}

/** 合并续取窗口并限制内存中的 K 线数量，避免无限平移使图表缓冲失控。 */
export function mergeCandleWindow(
  current: BacktestCandle[],
  incoming: BacktestCandle[],
  direction: 'before' | 'after' | null,
  maxCandles = MAX_LOADED_CANDLES,
): BacktestCandle[] {
  const byTime = new Map(current.map((candle) => [candle.time, candle]))
  incoming.forEach((candle) => byTime.set(candle.time, candle))
  const merged = [...byTime.values()].sort((left, right) => left.time - right.time)
  if (merged.length <= maxCandles) return merged
  if (direction === 'before') return merged.slice(0, maxCandles)
  if (direction === 'after') return merged.slice(-maxCandles)
  const start = Math.floor((merged.length - maxCandles) / 2)
  return merged.slice(start, start + maxCandles)
}
