import type { JsonValue } from '@/api/types'
import { formatLedgerDateTime } from '@/shared/time'

/** 「无数据 / 不可用」的统一占位符。不要在各处硬编码 '-' 或 '—'。 */
export const EMPTY_VALUE = '—'

type NumericLike = number | string | null | undefined

/** 数值或 null。后端 NUMERIC 字段常以字符串返回，这里统一收口。 */
export function toNumberOrNull(value: NumericLike): number | null {
  if (value === null || value === undefined || value === '') return null
  const numeric = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(numeric) ? numeric : null
}

/** 数值，缺失或非法时按 0 计。仅用于求和、比较等聚合场景。 */
export function asNumber(value: NumericLike): number {
  return toNumberOrNull(value) ?? 0
}

/**
 * 金额：固定保留 `digits` 位小数，用于对齐的金额列。
 * 与 {@link formatNumber} 的区别是会补零（`56` → `56.00`）。
 */
export function formatMoney(value: NumericLike, digits = 2): string {
  const numeric = toNumberOrNull(value)
  if (numeric === null) return EMPTY_VALUE
  return numeric.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  })
}

/**
 * 一般数值：最多保留 `digits` 位小数，不补零（`1000` → `1,000`）。
 * 与 {@link formatMoney} 的区别是不强制小数位。
 */
export function formatNumber(value: NumericLike, digits = 2): string {
  const numeric = toNumberOrNull(value)
  if (numeric === null) return EMPTY_VALUE
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: digits }).format(numeric)
}

/** 定点比值，例如盈亏比。总是保留 `digits` 位小数。 */
export function formatRatio(value: NumericLike, digits = 2): string {
  const numeric = toNumberOrNull(value)
  if (numeric === null) return EMPTY_VALUE
  return numeric.toFixed(digits)
}

/**
 * 比例转百分比。
 * 省略 `digits` 时最多两位且不补零（`0.6165` → `61.65%`、`5` → `500%`）；
 * 传入 `digits` 时固定该位数（`formatPercent(0.5, 0)` → `50%`）。
 */
export function formatPercent(value: NumericLike, digits?: number): string {
  const numeric = toNumberOrNull(value)
  if (numeric === null) return EMPTY_VALUE
  const percent = numeric * 100
  return digits === undefined ? `${formatNumber(percent, 2)}%` : `${percent.toFixed(digits)}%`
}

/** 秒数转人类可读时长。 */
export function formatDuration(seconds: NumericLike): string {
  const numeric = toNumberOrNull(seconds)
  if (numeric === null) return EMPTY_VALUE
  if (numeric < 60) return `${Math.round(numeric)}秒`
  if (numeric < 3600) return `${formatNumber(numeric / 60, 1)}分钟`
  if (numeric < 86400) return `${formatNumber(numeric / 3600, 1)}小时`
  return `${formatNumber(numeric / 86400, 1)}天`
}

/** 毫秒时长，复用 {@link formatDuration} 的表述，避免同一概念出现两种写法。 */
export function formatDurationMs(milliseconds: NumericLike): string {
  const numeric = toNumberOrNull(milliseconds)
  return numeric === null ? EMPTY_VALUE : formatDuration(numeric / 1000)
}

/** 时间：固定按账本时区渲染，缺失时返回统一占位符。 */
export function formatDateTime(value: string | number | null | undefined): string {
  return formatLedgerDateTime(value) ?? EMPTY_VALUE
}

/** 时间（不含秒）：固定按账本时区渲染。 */
export function formatDateTimeMinutes(value: string | number | null | undefined): string {
  return formatLedgerDateTime(value, { seconds: false }) ?? EMPTY_VALUE
}

/** 盈亏着色。0 与缺失都归为中性，避免把「无数据」显示成盈利或亏损。 */
export function pnlClass(value: NumericLike): string {
  const numeric = toNumberOrNull(value)
  if (numeric === null || numeric === 0) return 'value-neutral'
  return numeric > 0 ? 'value-positive' : 'value-negative'
}

/** 把交易方向与持仓方向统一显示成买卖。 */
export function sideLabel(value: string | null | undefined): string {
  const normalized = value?.toUpperCase()
  if (normalized === 'BUY' || normalized === 'LONG') return '买 / BUY'
  if (normalized === 'SELL' || normalized === 'SHORT') return '卖 / SELL'
  return value || EMPTY_VALUE
}

/** 按后端 schema 声明的类型渲染动态报表字段。 */
export function displayValue(value: JsonValue | undefined, type?: string): string {
  if (value === null || value === undefined || value === '') return EMPTY_VALUE
  if (type === 'datetime') return formatDateTime(value as string | number)
  if (type === 'percent' && typeof value === 'number') return formatPercent(value)
  if ((type === 'number' || type === 'price' || type === 'decimal') && typeof value === 'number') {
    return formatNumber(value, type === 'price' ? 8 : 6)
  }
  if (type === 'boolean' || typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'number') return formatNumber(value, 6)
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}
