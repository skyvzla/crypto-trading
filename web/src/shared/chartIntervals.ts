export const CHART_INTERVALS = ['1s', '1m', '5m', '15m', '1h', '4h', '6h', '8h', '12h', '1d'] as const

export type ChartInterval = (typeof CHART_INTERVALS)[number]

export const DEFAULT_CHART_INTERVAL: ChartInterval = '1s'

export function isChartInterval(value: string): value is ChartInterval {
  return (CHART_INTERVALS as readonly string[]).includes(value)
}
