export type ChartTheme = {
  background: string
  text: string
  axisText: string
  grid: string
  border: string
  up: string
  down: string
  signal: string
  filled: string
  pending: string
  average: string
  invalid: string
  areaLine: string
  areaTop: string
  areaBottom: string
}

const LIGHT_THEME: ChartTheme = {
  background: '#ffffff', text: '#334155', axisText: '#64748b', grid: '#e2e8f0', border: '#cbd5e1',
  up: '#059669', down: '#e11d48', signal: '#b45309', filled: '#ea580c', pending: '#2563eb', average: '#1e293b', invalid: '#dc2626',
  areaLine: '#16a34a', areaTop: 'rgba(22, 163, 74, .28)', areaBottom: 'rgba(22, 163, 74, .02)'
}

const DARK_THEME: ChartTheme = {
  background: '#111827', text: '#d7e0ee', axisText: '#94a8c1', grid: '#263243', border: '#41536b',
  up: '#34d399', down: '#fb7185', signal: '#fbbf24', filled: '#fb923c', pending: '#60a5fa', average: '#e2e8f0', invalid: '#f87171',
  areaLine: '#16a34a', areaTop: 'rgba(22, 163, 74, .28)', areaBottom: 'rgba(22, 163, 74, .02)'
}

function cssColor(name: string, fallback: string) {
  if (typeof document === 'undefined') return fallback
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value || fallback
}

export function getChartTheme(dark: boolean): ChartTheme {
  const fallback = dark ? DARK_THEME : LIGHT_THEME
  return {
    background: fallback.background,
    text: fallback.text,
    axisText: fallback.axisText,
    grid: cssColor('--chart-grid', fallback.grid),
    border: cssColor('--chart-border', fallback.border),
    up: fallback.up,
    down: fallback.down,
    signal: fallback.signal,
    filled: cssColor('--chart-filled', fallback.filled),
    pending: fallback.pending,
    average: fallback.average,
    invalid: fallback.invalid,
    areaLine: fallback.areaLine,
    areaTop: fallback.areaTop,
    areaBottom: fallback.areaBottom
  }
}
