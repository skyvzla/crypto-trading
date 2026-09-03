import type { BacktestEvent } from '@/api/types'
import { formatDateTime, formatDurationMs, formatNumber, formatPercent } from '@/shared/format'
import { reportLabel } from '@/features/backtests/reportLabels'

const EVENT_LABELS: Record<string, string> = {
  signal_triggered: '信号触发',
  signal_rejected: '信号拒绝',
  signal_expired: '信号过期',
  signal_invalidated: '信号失效',
  entry_plan_created: '入场计划创建',
  campaign_first_fill: '首笔成交',
  campaign_rotation_activated: '轮换交易激活',
  campaign_rotation_old_signal_closed: '轮换旧信号关闭',
  campaign_rotation_exit_requested: '轮换退出请求',
  campaign_rotation_exit_filled: '轮换退出成交',
  campaign_timeout_check: '持仓超时检查',
  campaign_timeout_exit_requested: '超时退出请求',
  campaign_timeout_exit_filled: '超时退出成交',
  candidate_early_risk_unlocked: '提前解锁风险检查',
  candidate_origin_check: '起涨点检查',
  candidate_exit_requested: '候选退出请求',
  candidate_exit_waiting_entry_cancel: '等待撤销入场单',
  candidate_exit_waiting_campaign_entries: '等待交易入场单结束',
  candidate_oi_stop_waiting_entry_cancel: '持仓量止损等待撤单',
  candidate_oi_stop_exit_requested: '持仓量止损退出请求',
  pullback_entry_placed: '回调入场挂单',
  pullback_entry_ready: '回调入场就绪',
  pullback_entry_filled: '回调入场成交',
  pullback_exit_requested: '回调策略退出请求',
  pullback_exit_filled: '回调策略退出成交',
  pullback_rejected_low_buy_ratio: '回调入场拒绝',
  pullback_order_expired: '回调入场单过期',
  pullback_timeout: '回调等待超时',
  pullback_data_gap: '回调行情缺口',
  pullback_next_open_invalid: '回调次根开盘失效',
  pullback_position_already_open: '回调仓位已存在',
  pullback_state_missing: '回调状态缺失',
  pullback_v22_invalid_price: '回调触及失效价',
  pullback_impulse_base_breached: '回调跌破脉冲起点',
  legacy_ambiguous_exit_requested: '旧版歧义退出请求',
  legacy_stop_exit_requested: '旧版止损退出请求',
  legacy_target_exit_requested: '旧版止盈退出请求',
  legacy_timeout_exit_requested: '旧版超时退出请求',
  research_hold_exit_requested: '研究持有退出请求',
  research_stop_exit_requested: '研究止损退出请求'
}

const FIELD_LABELS: Record<string, string> = {
  trigger_price: '触发价格', rise_5s: '5 秒涨幅', rise_10s: '10 秒涨幅', rise_15s: '15 秒涨幅', rise_60s: '60 秒涨幅',
  rise_threshold_5s: '5 秒涨幅门槛', volume_5s: '5 秒成交量', median_volume_1s: '1 秒成交量中位数',
  volume_multiple_5s: '5 秒量比', volume_threshold_5s: '5 秒量比门槛', rise_from_12h_low: '相对 12 小时低点涨幅',
  td_sell_setup_5m: '5 分钟 TD 卖出序列', td_sell_setup_15m: '15 分钟 TD 卖出序列',
  upper_wick_ratio_5m: '5 分钟上影线比例', upper_wick_ratio_15m: '15 分钟上影线比例', volume_multiple_5m: '5 分钟量比',
  spike_high: '尖峰高点', spike_high_time: '尖峰高点时间', origin_price: '起涨价格', origin_floor: '起涨价格下限',
  rise_low: '上涨窗口低点', invalid_price: '失效价格', pullback_threshold: '回调触发价', pullback_atr: '回调 ATR 倍数',
  tier_prices: '分档价格', tier_weights: '分档权重', active_time: '计划生效时间', expire_time: '计划失效时间',
  entry_pattern: '入场形态', pullback_before_fill: '成交前已回调', pullback_time: '回调时间', pullback_low: '回调低点',
  mark_price: '标记价格', gross_pnl: '毛盈亏', net_pnl: '净盈亏', entry_commission: '入场手续费', exit_required: '需要退出',
  gross_return: '毛收益率', threshold: '触发门槛', action: '执行动作', reason: '原因', rejection_stage: '拒绝环节', rejection_reasons: '拒绝原因',
  cancelled_orders: '已撤订单数', order_id: '订单 ID', fill_id: '成交 ID', quantity: '数量', price: '成交价格',
  first_fill_time: '首笔成交时间', origin_signal_time: '原信号时间', entry_tier_mode: '入场分档模式', exit_policy: '退出策略', strategy_version: '策略版本',
  scored_score: '准入评分', scored_threshold: '准入评分门槛', max_rise_5s: '5 秒最大涨幅', max_rise_window: '窗口最大涨幅',
  max_volume_multiple_5s: '5 秒最大量比', spike_avg_deviation_pct: '尖峰均价偏离', spike_vwap_deviation_pct: '尖峰 VWAP 偏离',
  oi: '持仓量', previous_oi: '前值持仓量', oi_change_pct: '持仓量变化', max_oi_change_pct: '持仓量变化上限',
  ls_ratio: '多空账户比', max_ls_ratio: '多空账户比上限', min_td_sell_setup_5m: '5 分钟 TD 卖出序列下限',
  min_volume_multiple_5m: '5 分钟量比下限', metrics_available_time: '指标可用时间', d_oi_pct: '确认期持仓量涨幅',
  oi_stop_oi_rise_pct: '持仓量止损涨幅门槛', loss_pct: '价格亏损幅度', oi_stop_loss_pct: '持仓量止损亏损门槛',
  hard_stop_loss_ratio: '硬止损实际亏损', hard_stop_loss_pct: '硬止损门槛', hard_stop_confirm_ms: '硬止损确认时长',
  candidate: '候选入场价', entry_price: '计划入场价', impulse_base_price: '脉冲起点价格', retrace_frac: '回撤比例',
  bar_low: '当前 K 线低点', buy_ratio_entry: '主动买入占比', buy_ratio_entry_min: '主动买入占比下限', wait_ms: '等待时长',
  timeout_stage: '超时阶段', observed_close: '观察收盘价', target_price: '目标价格', pnl_pct: '盈亏幅度',
  base_ms: '持仓量基准时间', confirm_ms: '持仓量确认时间', rise_60s_threshold: '60 秒涨幅门槛',
  decision: '决策', decay_agreement: '动能衰减一致度'
}

const MAJOR_FIELDS = new Set([
  'reason', 'rejection_reasons', 'action', 'trigger_price', 'price', 'mark_price',
  'rise_5s', 'rise_60s', 'volume_multiple_5s', 'rise_from_12h_low', 'scored_score',
  'spike_high', 'origin_price', 'pullback_threshold', 'invalid_price', 'tier_prices',
  'gross_return', 'gross_pnl', 'net_pnl', 'quantity', 'exit_required',
  'oi_change_pct', 'ls_ratio', 'd_oi_pct', 'loss_pct', 'candidate', 'entry_price',
  'impulse_base_price', 'bar_low', 'buy_ratio_entry', 'observed_close', 'target_price'
])

const REFERENCE_FIELDS: Record<string, string[]> = {
  rise_5s: ['rise_threshold_5s', 'max_rise_5s', 'max_rise_window'],
  volume_multiple_5s: ['volume_threshold_5s', 'max_volume_multiple_5s'],
  rise_60s: ['rise_60s_threshold'],
  oi_change_pct: ['max_oi_change_pct'],
  ls_ratio: ['max_ls_ratio'],
  td_sell_setup_5m: ['min_td_sell_setup_5m'],
  volume_multiple_5m: ['min_volume_multiple_5m'],
  d_oi_pct: ['oi_stop_oi_rise_pct'],
  loss_pct: ['oi_stop_loss_pct'],
  buy_ratio_entry: ['buy_ratio_entry_min'],
  gross_return: ['threshold'],
  hard_stop_loss_ratio: ['hard_stop_loss_pct'],
  scored_score: ['scored_threshold'],
  spike_avg_deviation_pct: ['spike_avg_deviation_max_pct'],
  spike_range_pct: ['spike_range_max_pct'],
  spike_vwap_deviation_pct: ['spike_vwap_deviation_max_pct'],
  box_break_minutes: ['box_duration_min_minutes'],
  consecutive_up_minutes: ['max_consecutive_up_minutes']
}

const TIME_FIELDS = /(?:^|_)(?:time|at)$/
const DURATION_MS_FIELDS = /_ms$/
const RAW_PERCENT_FIELDS = /_pct$/
const PRICE_FIELDS = /(?:^|_)(?:price|prices|high|low|floor|atr)(?:_|$)/
const MULTIPLE_FIELDS = /multiple/
const CODE_VALUE_FIELDS = new Set(['action', 'decision', 'reason', 'rejection_stage', 'rejection_reasons', 'entry_pattern', 'entry_tier_mode', 'exit_policy'])

const CODE_VALUE_LABELS: Record<string, string> = {
  post_base_entry_filters: '基础条件通过后的入场过滤',
  base_trigger_filters: '基础信号触发过滤',
  hard_stop: '硬止损', time_risk: '时间风险', momentum_risk: '动量风险',
  profit_drawdown: '盈利回撤', gate_stop: '门禁止损', trend_exit: '趋势退出',
  hold: '继续持有', exit: '退出', reduce_half: '减半仓位',
  origin_momentum_continues: '起涨点动能延续', origin_momentum_decay: '起涨点动能衰减',
  short_term_high_pullback_rebreak: '短期高点回调再突破',
  direct_entry_without_pullback: '未回调直接入场', single_entry: '单档入场',
  box_data_insufficient: '箱体数据不足', max_consecutive_up_minutes: '连续上涨时间超限',
  spike_avg_deviation_max_pct: '尖峰均价偏离超限', spike_vwap_deviation_max_pct: '尖峰 VWAP 偏离超限',
  origin_floor: '低于起涨价格下限', prior_high: '未超过前期高点', entry_scoring_threshold: '准入评分不足'
}

function isRatioField(key: string): boolean {
  return /(?:^|_)return(?:_|$)/.test(key)
    || /^rise_(?:threshold_)?\d+[smhd]$/.test(key)
    || /^rise_\d+[smhd]_threshold$/.test(key)
    || /^max_rise_(?:\d+[smhd]|window)$/.test(key)
    || /^rise_from_.+_low$/.test(key)
    || /^group_rise_.+_threshold$/.test(key)
    || /^accel_rise_.+_(?:min|max)$/.test(key)
    || /^upper_wick_ratio_/.test(key)
    || /(?:^|_)buy_ratio(?:_|$)/.test(key)
    || /(?:^|_)loss_ratio(?:_|$)/.test(key)
    || key === 'rise_window'
    || key === 'tier_weights'
    || key === 'retrace_frac'
    || key === 'min_spike_rise'
    || key === 'hard_stop_loss_pct'
}

export type EventParameterGroupKey = 'price' | 'rise_volume' | 'oi' | 'risk_execution' | 'decision' | 'other'

const FIELD_GROUP_MAP: Record<string, EventParameterGroupKey> = {
  // 决策与状态
  action: 'decision',
  decision: 'decision',
  reason: 'decision',
  rejection_stage: 'decision',
  rejection_reasons: 'decision',
  cancelled_orders: 'decision',
  exit_required: 'decision',

  // 价格与标线
  trigger_price: 'price',
  price: 'price',
  fill_price: 'price',
  mark_price: 'price',
  spike_high: 'price',
  spike_high_time: 'price',
  origin_price: 'price',
  origin_floor: 'price',
  rise_low: 'price',
  invalid_price: 'price',
  pullback_threshold: 'price',
  pullback_atr: 'price',
  pullback_low: 'price',
  pullback_time: 'price',
  candidate: 'price',
  entry_price: 'price',
  impulse_base_price: 'price',
  bar_low: 'price',
  observed_close: 'price',
  target_price: 'price',
  tier_prices: 'price',

  // 涨幅与量比
  rise_5s: 'rise_volume',
  rise_10s: 'rise_volume',
  rise_15s: 'rise_volume',
  rise_60s: 'rise_volume',
  rise_from_12h_low: 'rise_volume',
  volume_5s: 'rise_volume',
  median_volume_1s: 'rise_volume',
  volume_multiple_5s: 'rise_volume',
  max_rise_5s: 'rise_volume',
  max_rise_window: 'rise_volume',
  max_volume_multiple_5s: 'rise_volume',
  volume_multiple_5m: 'rise_volume',
  min_volume_multiple_5m: 'rise_volume',
  upper_wick_ratio_5m: 'rise_volume',
  upper_wick_ratio_15m: 'rise_volume',
  td_sell_setup_5m: 'rise_volume',
  td_sell_setup_15m: 'rise_volume',
  spike_avg_deviation_pct: 'rise_volume',
  spike_vwap_deviation_pct: 'rise_volume',
  decay_agreement: 'rise_volume',

  // 持仓量与多空
  oi: 'oi',
  previous_oi: 'oi',
  oi_change_pct: 'oi',
  max_oi_change_pct: 'oi',
  ls_ratio: 'oi',
  max_ls_ratio: 'oi',
  d_oi_pct: 'oi',
  base_ms: 'oi',
  confirm_ms: 'oi',

  // 风控与执行
  scored_score: 'risk_execution',
  scored_threshold: 'risk_execution',
  loss_pct: 'risk_execution',
  oi_stop_loss_pct: 'risk_execution',
  oi_stop_oi_rise_pct: 'risk_execution',
  hard_stop_loss_ratio: 'risk_execution',
  hard_stop_loss_pct: 'risk_execution',
  hard_stop_confirm_ms: 'risk_execution',
  buy_ratio_entry: 'risk_execution',
  buy_ratio_entry_min: 'risk_execution',
  retrace_frac: 'risk_execution',
  gross_pnl: 'risk_execution',
  net_pnl: 'risk_execution',
  gross_return: 'risk_execution',
  pnl_pct: 'risk_execution',
  entry_commission: 'risk_execution',
  quantity: 'risk_execution',
  order_id: 'risk_execution',
  fill_id: 'risk_execution',
  tier_weights: 'risk_execution',
  active_time: 'risk_execution',
  expire_time: 'risk_execution',
  first_fill_time: 'risk_execution',
  origin_signal_time: 'risk_execution',
  entry_pattern: 'risk_execution',
  entry_tier_mode: 'risk_execution',
  exit_policy: 'risk_execution',
  pullback_before_fill: 'risk_execution',
  timeout_stage: 'risk_execution',
  wait_ms: 'risk_execution',
  strategy_version: 'risk_execution',
  metrics_available_time: 'risk_execution'
}

const GROUP_TITLES: Record<EventParameterGroupKey, string> = {
  price: '价格与标线',
  rise_volume: '涨幅与量比',
  oi: '持仓量与多空',
  risk_execution: '风控与执行',
  decision: '决策与状态',
  other: '其他参数'
}

const GROUP_ORDER: EventParameterGroupKey[] = ['price', 'rise_volume', 'oi', 'risk_execution', 'decision', 'other']

export function classifyParameterGroup(key: string): EventParameterGroupKey {
  if (FIELD_GROUP_MAP[key]) return FIELD_GROUP_MAP[key]
  if (PRICE_FIELDS.test(key)) return 'price'
  if (isRatioField(key) || MULTIPLE_FIELDS.test(key)) return 'rise_volume'
  return 'other'
}

export interface EventParameterRow {
  key: string
  label: string
  value: string
  reference: string
  major: boolean
}

export interface EventParameterGroup {
  id: EventParameterGroupKey
  title: string
  rows: EventParameterRow[]
}

export function eventDisplayName(event: Pick<BacktestEvent, 'type' | 'title'>): string {
  const type = event.type || String(event.title || '')
  const translated = EVENT_LABELS[type] || reportLabel(type)
  const label = translated === '字段' ? '策略事件' : translated
  return `${label}（${type || 'unknown'}）`
}

export function eventParameterLabel(key: string): string {
  const translated = FIELD_LABELS[key] || reportLabel(key)
  return translated === '字段' ? key : translated
}

export function resolvePricePrecision(
  _symbol?: string,
  samplePrices: Array<number | string | null | undefined> = []
): number {
  let maxDecimals = 0
  let hasValid = false
  for (const price of samplePrices) {
    if (price == null || price === '') continue
    const num = Number(price)
    if (!Number.isFinite(num) || num === 0) continue
    hasValid = true
    const str = String(price).trim()
    const dot = str.indexOf('.')
    if (dot !== -1) {
      const decimals = str.slice(dot + 1).replace(/0+$/, '').length
      if (decimals > maxDecimals) maxDecimals = decimals
    }
  }
  if (hasValid) {
    return Math.min(8, Math.max(2, maxDecimals))
  }
  return 2
}

function formatArray(key: string, values: unknown[], pricePrecision?: number): string {
  if (!values.length) return '-'
  return values.map((value, index) => {
    const formatted = formatEventValue(key, value, pricePrecision)
    return key === 'tier_prices' || key === 'tier_weights' ? `${index + 1}档 ${formatted}` : formatted
  }).join('；')
}

function formatCodeValue(value: unknown): string {
  const code = String(value)
  const translated = CODE_VALUE_LABELS[code] || reportLabel(code)
  return translated === '字段' ? code : `${translated}（${code}）`
}

export function formatEventValue(key: string, value: unknown, pricePrecision?: number): string {
  if (value == null || value === '') return '-'
  if (Array.isArray(value)) return CODE_VALUE_FIELDS.has(key)
    ? value.map(formatCodeValue).join('；') || '-'
    : formatArray(key, value, pricePrecision)
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'object') {
    return Object.entries(value as Record<string, unknown>)
      .map(([childKey, childValue]) => `${eventParameterLabel(childKey)}：${formatEventValue(childKey, childValue, pricePrecision)}`)
      .join('；') || '-'
  }
  if (CODE_VALUE_FIELDS.has(key)) return formatCodeValue(value)
  if ((TIME_FIELDS.test(key) || key === 'base_ms' || key === 'confirm_ms') && Number.isFinite(Number(value))) return formatDateTime(Number(value))
  if (DURATION_MS_FIELDS.test(key) && Number.isFinite(Number(value))) return formatDurationMs(Number(value))
  if (isRatioField(key) && Number.isFinite(Number(value))) return formatPercent(Number(value), 2)
  if (RAW_PERCENT_FIELDS.test(key) && Number.isFinite(Number(value))) return `${formatNumber(Number(value), 4)}%`
  if ((PRICE_FIELDS.test(key) || key === 'candidate' || key === 'observed_close') && Number.isFinite(Number(value))) {
    if (pricePrecision != null) {
      return formatNumber(Number(value), pricePrecision)
    }
    const s = String(value).trim()
    const dot = s.indexOf('.')
    const naturalDigits = dot === -1 ? 2 : Math.min(8, Math.max(2, s.slice(dot + 1).replace(/0+$/, '').length))
    return formatNumber(Number(value), naturalDigits)
  }
  if (MULTIPLE_FIELDS.test(key) && Number.isFinite(Number(value))) return formatNumber(Number(value), 2)
  return String(value)
}

export function eventParameterRows(
  event: Pick<BacktestEvent, 'data' | 'price'> & Partial<Pick<BacktestEvent, 'type'>>,
  referenceData: Record<string, unknown> = {},
  pricePrecision?: number
): EventParameterRow[] {
  const data: Record<string, unknown> = { ...(event.data || {}) }
  if (event.price != null && data.price == null && data.trigger_price == null && data.fill_price == null) data.price = event.price
  const consumedReferences = new Set<string>()
  const rows = Object.entries(data).map(([key, value], sourceIndex) => {
    const referenceKeys = (REFERENCE_FIELDS[key] || []).filter((candidate) => data[candidate] != null || referenceData[candidate] != null)
    referenceKeys.filter((referenceKey) => data[referenceKey] != null).forEach((referenceKey) => consumedReferences.add(referenceKey))
    return {
      key,
      label: key === 'price' && event.type?.includes('exit_requested')
        ? '退出触发价格'
        : key === 'price' && event.type?.includes('entry')
          ? '入场价格'
          : eventParameterLabel(key),
      value: formatEventValue(key, value, pricePrecision),
      reference: referenceKeys.length
        ? referenceKeys.map((referenceKey) => `${eventParameterLabel(referenceKey)}：${formatEventValue(referenceKey, data[referenceKey] ?? referenceData[referenceKey], pricePrecision)}`).join('；')
        : '-',
      major: MAJOR_FIELDS.has(key),
      sourceIndex
    }
  }).filter((row) => !consumedReferences.has(row.key))
  return rows
    .sort((left, right) => Number(right.major) - Number(left.major) || left.sourceIndex - right.sourceIndex)
    .map(({ sourceIndex: _sourceIndex, ...row }) => row)
}

export function eventParameterGroups(
  event: Pick<BacktestEvent, 'data' | 'price'> & Partial<Pick<BacktestEvent, 'type'>>,
  referenceData: Record<string, unknown> = {},
  pricePrecision?: number
): EventParameterGroup[] {
  const rows = eventParameterRows(event, referenceData, pricePrecision)
  const groupedMap = new Map<EventParameterGroupKey, EventParameterRow[]>()

  for (const row of rows) {
    const groupKey = classifyParameterGroup(row.key)
    const list = groupedMap.get(groupKey) || []
    list.push(row)
    groupedMap.set(groupKey, list)
  }

  const result: EventParameterGroup[] = []
  for (const groupKey of GROUP_ORDER) {
    const groupRows = groupedMap.get(groupKey)
    if (groupRows && groupRows.length > 0) {
      result.push({
        id: groupKey,
        title: GROUP_TITLES[groupKey],
        rows: groupRows
      })
    }
  }
  return result
}
