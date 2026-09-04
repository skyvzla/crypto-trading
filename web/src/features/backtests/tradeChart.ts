import type { BacktestFill, BacktestOrder, JsonObject } from '@/api/types'

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
