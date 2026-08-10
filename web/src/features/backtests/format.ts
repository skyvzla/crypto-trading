import type { JsonValue } from '@/api/types'

type NumericLike = number | string | null | undefined

function numericValue(value: NumericLike): number | null {
  if (value === null || value === undefined || value === '') return null
  const numeric = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(numeric) ? numeric : null
}

export function timestampMs(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null
  if (typeof value === 'number') return value < 10_000_000_000 ? value * 1000 : value
  const numeric = Number(value)
  if (Number.isFinite(numeric)) return numeric < 10_000_000_000 ? numeric * 1000 : numeric
  const parsed = Date.parse(value)
  return Number.isNaN(parsed) ? null : parsed
}

export function formatTime(value: string | number | null | undefined): string {
  const ms = timestampMs(value)
  if (ms === null) return '-'
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
  }).format(ms)
}

export function formatNumber(value: NumericLike, digits = 2): string {
  const numeric = numericValue(value)
  if (numeric === null) return '-'
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: digits }).format(numeric)
}

export function formatPercent(value: NumericLike): string {
  const numeric = numericValue(value)
  if (numeric === null) return '-'
  const normalized = Math.abs(numeric) <= 1 ? numeric * 100 : numeric
  return `${formatNumber(normalized, 2)}%`
}

export function formatDuration(value: NumericLike): string {
  const seconds = numericValue(value)
  if (seconds === null) return '-'
  if (seconds < 60) return `${Math.round(seconds)}秒`
  if (seconds < 3600) return `${formatNumber(seconds / 60, 1)}分钟`
  if (seconds < 86400) return `${formatNumber(seconds / 3600, 1)}小时`
  return `${formatNumber(seconds / 86400, 1)}天`
}

export function displayValue(value: JsonValue | undefined, type?: string): string {
  if (value === null || value === undefined || value === '') return '-'
  if (type === 'datetime') return formatTime(value as string | number)
  if (type === 'percent' && typeof value === 'number') return formatPercent(value)
  if ((type === 'number' || type === 'price' || type === 'decimal') && typeof value === 'number') return formatNumber(value, type === 'price' ? 8 : 6)
  if (type === 'boolean' || typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'number') return formatNumber(value, 6)
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

export function pnlClass(value: NumericLike): string {
  const numeric = numericValue(value)
  if (numeric === null || numeric === 0) return ''
  return numeric > 0 ? 'value-positive' : 'value-negative'
}
