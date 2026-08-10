// 后端接口仍在调整，这里只固化当前 /api/v1 已存在且稳定的字段。
// Decimal 在 JSON 中是字符串，保持 string 以避免精度丢失。

export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface Health {
  status: string
  service: string
  timestamp: string
}

export interface LedgerFilters {
  account_id?: string
  strategy_id?: string
  symbol?: string
}

export interface ExchangeSymbol {
  symbol: string
  pair: string
  contract_type: string
  status: string
  onboard_date: string | null
  delivery_date: string | null
  base_asset: string | null
  quote_asset: string | null
  margin_asset: string | null
  underlying_type: string | null
  active: boolean
  synced_at: string
  global_enabled: boolean
  global_admission_version: number
}

export interface ExchangeCategory {
  category_key: string
  source: string
  category_type: 'CATEGORY' | 'SUBCATEGORY'
  code: string
  name: string
  parent_key: string | null
  active: boolean
  synced_at: string
}

export interface StrategyCategoryAdmission {
  strategy_id: string
  category_key: string
  enabled: boolean
  version: number
  updated_at: string
  updated_by: string
  reason: string | null
}

export type JsonValue = string | number | boolean | null | JsonValue[] | JsonObject
export interface JsonObject { [key: string]: JsonValue }

export interface BacktestResearch {
  id: string
  name: string
  strategy_id: string
  strategy_version?: string | null
  status: string
  start_time?: string | number | null
  end_time?: string | number | null
  symbol_count?: number
  run_count?: number
  trade_count?: number
  win_rate?: number | null
  net_pnl?: number | null
  created_at: string | number
  data_source?: string | null
  parameters?: JsonObject
}

export interface ReportDescriptor {
  type: string
  title: string
  description?: string | null
  row_count?: number
  category?: string | null
  updated_at?: string | number | null
}

export interface ReportColumn {
  key: string
  title?: string
  label?: string
  type?: 'string' | 'number' | 'boolean' | 'datetime' | 'json'
  sortable?: boolean
}

export interface BacktestReportPage {
  descriptor: ReportDescriptor
  columns: Array<ReportColumn | string>
  rows: JsonObject[]
  total: number
  limit: number
  offset: number
}

export interface BacktestSymbolSummary {
  symbol: string
  trade_count: number
  win_rate?: number | null
  net_pnl?: number | null
  average_win?: number | null
  average_loss?: number | null
  max_profit?: number | null
  max_loss?: number | null
  average_holding_seconds?: number | null
  full_tier_fill_rate?: number | null
  run_count?: number
  metrics?: JsonObject
}

export interface BacktestTradeSummary {
  id: string
  trade_id?: string
  symbol: string
  side?: string
  signal_time?: string | number | null
  entry_time: string | number
  entry_price: number
  exit_time?: string | number | null
  exit_price?: number | null
  net_pnl: number
  net_return?: number | null
  winner?: boolean
  exit_reason?: string | null
  filled_tier_count?: number | null
  holding_seconds?: number | null
  run_id?: string | null
  parameters?: JsonObject
  metrics?: JsonObject
}

export interface BacktestOrder {
  id?: string
  tier?: number | null
  price: number
  quantity?: number | null
  status?: string | null
  created_time?: string | number | null
}

export interface BacktestFill {
  id?: string
  tier?: number | null
  time: string | number
  price: number
  quantity?: number | null
  side?: string | null
}

export interface ChartOverlay {
  key: string
  label?: string
  kind?: 'price_line' | 'marker'
  color?: string
  line_style?: 'solid' | 'dashed' | 'dotted' | number
}

export interface StrategyField {
  key: string
  label?: string
  type?: string
  format?: string
  visible?: boolean
}

export interface StrategyGroup {
  key: string
  label?: string
  fields: StrategyField[]
}

export interface BacktestStrategyDescriptor {
  strategy_id: string
  label?: string
  name?: string
  parameter_fields?: StrategyField[]
  detail_groups?: StrategyGroup[]
  groups?: StrategyGroup[]
  fields?: StrategyField[]
  chart_overlays?: ChartOverlay[]
}

export interface BacktestTradeDetail extends BacktestTradeSummary {
  research_id?: string
  strategy_id: string
  signal_price?: number | null
  average_entry_price?: number | null
  invalid_price?: number | null
  orders?: BacktestOrder[]
  fills?: BacktestFill[]
  tier_prices?: number[]
  attributes?: JsonObject
  strategy_data?: JsonObject
  metrics?: JsonObject
  parameters?: JsonObject
}

export interface BacktestEvent {
  id?: string
  time: string | number
  type: string
  title?: string | null
  description?: string | null
  price?: number | null
  data?: JsonObject
}

export interface BacktestCandle {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface BacktestCandlesResponse {
  symbol: string
  interval: string
  source: 'binance' | 'archive'
  candles: BacktestCandle[]
}
