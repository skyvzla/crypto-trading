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

export interface PageParams {
  limit?: number
  offset?: number
}

export interface ExchangeSymbolQuery extends PageParams {
  unclassified?: boolean
}

export interface OrderQuery extends LedgerFilters, PageParams {
  status?: string
  active_only?: boolean
}

/** 后端接受的日界线取值。请求侧统一用这个别名，不要再抄字面量联合。 */
export type LedgerTimezone = 'UTC' | 'Asia/Shanghai'

export interface TradeQuery extends LedgerFilters, PageParams {
  campaign_id?: string
  start_date?: string
  end_date?: string
  timezone?: LedgerTimezone
}

export interface CampaignQuery extends LedgerFilters, PageParams {
  campaign_id?: string
  start_date?: string
  end_date?: string
  timezone?: LedgerTimezone
}

export interface PositionQuery extends LedgerFilters, PageParams {}

export interface RuntimeStatusQuery extends PageParams {
  account_id?: string
  strategy_id?: string
}

export interface StrategyCapitalStatusQuery {
  account_id: string
  strategy_id: string
}

export interface PnLQuery extends LedgerFilters {
  account_id: string
}

export interface DailyPnLQuery extends LedgerFilters {
  start_date: string
  end_date: string
  timezone?: LedgerTimezone
}

export interface LedgerAccount {
  account_id: string
}

export interface PerformanceQuery extends LedgerFilters {
  account_id: string
  start_date: string
  end_date: string
  timezone?: LedgerTimezone
}

export interface AdmissionUpdate {
  enabled: boolean
  expected_version: number
  updated_by: string
  reason?: string | null
}

export interface LedgerOrder {
  id: number
  account_id: string
  strategy_id: string
  symbol: string
  order_id: string
  client_order_id: string
  campaign_id: string | null
  side: string
  order_type: string
  position_side: string | null
  quantity: string
  price: string | null
  stop_price: string | null
  status: string
  filled_quantity: string
  avg_fill_price: string | null
  commission: string | null
  commission_asset: string | null
  created_at: string
  updated_at: string
  exchange_created_at: string | null
  filled_at: string | null
}

export interface LedgerTrade {
  id: number
  account_id: string
  strategy_id: string
  symbol: string
  trade_id: string
  order_id: string
  client_order_id: string
  campaign_id: string | null
  side: string
  position_side: string | null
  quantity: string
  price: string
  quote_quantity: string
  commission: string
  commission_asset: string
  realized_pnl: string | null
  is_maker: boolean
  created_at: string
  exchange_time: string
}

export interface LedgerPosition {
  id: number
  account_id: string
  strategy_id: string
  symbol: string
  position_side: string
  quantity: string
  entry_price: string
  mark_price: string | null
  unrealized_pnl: string | null
  liquidation_price: string | null
  leverage: number | null
  margin_type: string | null
  isolated_margin: string | null
  exchange_time: string | null
  updated_at: string
}

export interface PnLSummary {
  account_id: string
  strategy_id: string | null
  symbol: string | null
  total_trades: number
  total_commission: string
  total_realized_pnl: string
  total_unrealized_pnl: string
  net_pnl: string
  win_count: number
  loss_count: number
  win_rate: number
  avg_win: string
  avg_loss: string
}

export interface DailyPnL {
  date: string
  account_id: string | null
  strategy_id: string | null
  symbol: string | null
  timezone: LedgerTimezone
  campaign_count: number
  fill_count: number
  trade_count: number
  realized_trade_count: number
  gross_realized_pnl: string
  total_commission: string
  commission_asset: string | null
  net_pnl: string | null
  funding_fee: string | null
  net_pnl_scope: string
}

export interface PerformanceSummary {
  account_id: string
  strategy_id: string | null
  symbol: string | null
  start_date: string | null
  end_date: string | null
  timezone: string
  total_trades: number
  total_fills: number
  win_count: number
  loss_count: number
  flat_count: number
  win_rate: number
  avg_win: string
  avg_loss: string
  payoff_ratio: string | null
  expectancy: string
  profit_factor: string | null
  total_commission: string
  total_realized_pnl: string
  net_pnl: string
  max_drawdown: string
  candidate_campaigns: number
  excluded_campaigns: number
  unattributed_fills: number
  metric_scope: string
}

export type PerformanceDimension = 'symbol' | 'category' | 'subcategory' | 'side' | 'exit_reason'

export interface PerformanceBreakdownQuery extends PerformanceQuery {
  group_by: PerformanceDimension
  category_key?: string
  subcategory_key?: string
  side?: 'LONG' | 'SHORT'
  exit_reason?: string
}

export interface PerformanceBreakdownItem {
  dimension_key: string | null
  dimension_label: string | null
  total_trades: number
  total_fills: number
  win_count: number
  loss_count: number
  flat_count: number
  win_rate: number
  avg_win: string
  avg_loss: string
  payoff_ratio: string | null
  expectancy: string
  profit_factor: string | null
  total_commission: string
  total_realized_pnl: string
  net_pnl: string
  max_drawdown: string
  candidate_campaigns: number
  excluded_campaigns: number
}

export interface PerformanceBreakdownResponse {
  account_id: string
  strategy_id: string | null
  symbol: string | null
  category_key: string | null
  subcategory_key: string | null
  side: string | null
  start_date: string
  end_date: string
  timezone: string
  group_by: PerformanceDimension
  dimension_available: boolean
  dimension_note: string | null
  available_dimensions: PerformanceDimension[]
  items: PerformanceBreakdownItem[]
  metric_scope: string
}

export interface CampaignPnL {
  account_id: string
  strategy_id: string
  symbol: string
  campaign_id: string
  trade_count: number
  sell_quantity: string
  sell_avg_price: string | null
  buy_quantity: string
  buy_avg_price: string | null
  total_commission: string
  commission_asset: string | null
  gross_realized_pnl: string
  net_realized_pnl: string
  remaining_quantity: string
  has_open_quantity: boolean
  acquired_at: string | null
  first_fill_at: string
  last_fill_at: string
  closed_at: string | null
  released_at: string | null
  lifecycle_duration_ms: number | null
}

export interface CampaignSummary {
  account_id: string
  strategy_id: string
  symbol: string
  campaign_id: string
  side: string | null
  fill_count: number
  sell_quantity: string
  buy_quantity: string
  total_commission: string | null
  commission_asset: string | null
  gross_realized_pnl: string
  net_realized_pnl: string | null
  first_fill_at: string
  last_fill_at: string
  closed_at: string | null
  has_open_quantity: boolean
  pnl_facts_complete: boolean
}

export interface CampaignPage extends Page<CampaignSummary> {
  unattributed_fills: number
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
  symbol_count: number
}

export interface SymbolGlobalAdmission {
  symbol: string
  enabled: boolean
  version: number
  explicit: boolean
  updated_at: string | null
  updated_by: string | null
  reason: string | null
}

export interface SymbolGlobalAdmissionAudit {
  id: number
  symbol: string
  previous_enabled: boolean | null
  enabled: boolean
  version: number
  changed_at: string
  changed_by: string
  reason: string | null
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

export interface StrategyCategoryAdmissionAudit {
  id: number
  strategy_id: string
  category_key: string
  previous_enabled: boolean | null
  enabled: boolean
  version: number
  changed_at: string
  changed_by: string
  reason: string | null
}

export interface StrategyAuditEvent {
  id: number
  event_key: string
  account_id: string
  event_time: number
  event_type: string
  symbol: string
  strategy_id: string
  campaign_id: string | null
  details: JsonObject
  created_at: string
}

export interface StrategyRuntimeStatus {
  account_id: string
  strategy_id: string
  instance_id: string
  mode: string
  status: string
  effective_status: string
  entry_enabled: boolean
  halted: boolean
  halt_reason: string | null
  gate_conditions: JsonObject
  started_at: string
  heartbeat_at: string
  stopped_at: string | null
}

export interface StrategyCapitalStatus {
  account_id: string
  strategy_id: string
  account_capital: string
  trading_capital: string
  reserve_capital: string
  minimum: string
  profit_reinvest_ratio: string
  capital_breached: boolean
  version: number
  updated_at: string
}

export interface UniversePreviewItem {
  symbol: string
  effective: boolean
  exclusion_reasons: string[]
  blocked_category_keys: string[]
}

export interface UniversePreview {
  strategy_id: string
  freeze_days: number
  total_symbols: number
  effective_symbols: number
  excluded_symbols: number
  total: number
  items: UniversePreviewItem[]
  limit: number
  offset: number
}

export interface UniversePreviewQuery extends PageParams {
  freeze_days?: number
  effective?: boolean
}

export interface ExchangeSymbolSyncStatus {
  initialized: boolean
  status: string
  last_attempt_at: string | null
  last_success_at: string | null
  synced_symbols: number
  last_error: string | null
  stale: boolean
  effective_universe_ready: boolean
  max_age_hours: number
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
