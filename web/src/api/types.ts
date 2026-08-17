// 后端接口仍在调整，这里只固化当前 /api/v1 已存在且稳定的字段。
// Decimal 在 JSON 中是字符串，保持 string 以避免精度丢失。

export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export type NotificationConnectorType = 'telegram' | 'webhook'
export type NotificationSeverity = 'info' | 'warning' | 'critical'
export type NotificationRoutingStatus = 'pending' | 'routed' | 'suppressed' | 'unrouted' | 'targeted'
export type NotificationDeliveryStatus = 'pending' | 'sending' | 'retry' | 'sent' | 'dead'

export interface NotificationConnector {
  id: string
  name: string
  type: NotificationConnectorType
  secret_ref: string | null
  config: JsonObject
  enabled: boolean
  version: number
  created_at: string
  updated_at: string
}

export interface NotificationEndpoint {
  id: string
  connector_id: string
  name: string
  address: string
  config: JsonObject
  enabled: boolean
  version: number
  created_at: string
  updated_at: string
}

export interface NotificationGroup {
  id: string
  name: string
  description: string | null
  enabled: boolean
  version: number
  endpoint_ids: string[]
  created_at: string
  updated_at: string
}

export interface NotificationPolicy {
  id: string
  name: string
  event_pattern: string
  severity: NotificationSeverity
  priority: number
  suppress: boolean
  enabled: boolean
  version: number
  group_ids: string[]
  created_at: string
  updated_at: string
}

export interface NotificationEvent {
  id: string
  event_type: string
  severity: NotificationSeverity
  source: string
  title: string
  body: string
  payload: JsonObject
  idempotency_key: string
  correlation_id: string | null
  fingerprint: string | null
  matched_policy_id: string | null
  routing_status: NotificationRoutingStatus
  occurred_at: string
  expires_at: string | null
  created_at: string
}

export interface NotificationDelivery {
  id: string
  event_id: string
  endpoint_id: string
  connector_snapshot: JsonObject
  endpoint_snapshot: JsonObject
  status: NotificationDeliveryStatus
  attempt_count: number
  next_attempt_at: string
  lease_until: string | null
  lease_owner: string | null
  last_error: string | null
  provider_message_id: string | null
  created_at: string
  updated_at: string
  sent_at: string | null
}

export interface NotificationPublishResponse {
  event: NotificationEvent
  deliveries: NotificationDelivery[]
  created: boolean
}

export interface NotificationOverview {
  connectors: number
  enabled_connectors: number
  endpoints: number
  enabled_endpoints: number
  groups: number
  policies: number
  events: number
  unrouted_events: number
  deliveries: Record<NotificationDeliveryStatus, number>
}

export interface NotificationConnectorInput {
  name: string
  type: NotificationConnectorType
  secret_ref?: string | null
  config?: JsonObject
  enabled: boolean
}

export interface NotificationEndpointInput {
  connector_id: string
  name: string
  address: string
  config?: JsonObject
  enabled: boolean
}

export interface NotificationGroupInput {
  name: string
  description?: string | null
  endpoint_ids: string[]
  enabled: boolean
}

export interface NotificationPolicyInput {
  name: string
  event_pattern: string
  severity: NotificationSeverity
  priority: number
  suppress: boolean
  group_ids: string[]
  enabled: boolean
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
  campaign_id?: string | null
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

export interface BacktestEquityTrade extends BacktestTradeSummary {
  gross_pnl?: number | null
  commission?: number | null
  entry_notional?: number | null
  gross_return?: number | null
}

export interface BacktestReplayParameterSet {
  parameters: JsonObject
  trade_count: number
  net_pnl: number
}

export interface BacktestReplayTradesResponse {
  parameters: JsonObject
  items: BacktestEquityTrade[]
}

export interface BacktestOrder {
  id: string
  tier?: number | null
  price: number
  quantity?: number | null
  status?: string | null
  created_time?: string | number | null
}

export interface BacktestFill {
  id: string
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
  id: number
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
