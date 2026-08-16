export function asNumber(value: string | number | null | undefined): number {
  if (value === null || value === undefined || value === '') return 0
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

export function formatMoney(
  value: string | number | null | undefined,
  digits = 2
): string {
  if (value === null || value === undefined || value === '') return '—'
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return '—'
  return parsed.toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  })
}

export function formatRatio(
  value: string | number | null | undefined,
  digits = 2
): string {
  if (value === null || value === undefined || value === '') return '—'
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return '—'
  return parsed.toFixed(digits)
}

export function formatPercent(
  value: string | number | null | undefined,
  digits = 1
): string {
  if (value === null || value === undefined || value === '') return '—'
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return '—'
  return `${(parsed * 100).toFixed(digits)}%`
}

export function formatDateTime(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.valueOf())) return String(value)
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  }).format(parsed)
}

export function pnlClass(value: string | number | null | undefined): string {
  const parsed = asNumber(value)
  if (parsed > 0) return 'value-positive'
  if (parsed < 0) return 'value-negative'
  return 'value-neutral'
}

export function sideLabel(value: string | null | undefined): string {
  const normalized = value?.toUpperCase()
  if (normalized === 'BUY' || normalized === 'LONG') return '多 / BUY'
  if (normalized === 'SELL' || normalized === 'SHORT') return '空 / SELL'
  return value || '—'
}
