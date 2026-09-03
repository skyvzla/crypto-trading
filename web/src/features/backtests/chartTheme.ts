/**
 * 图表画布调色板。
 *
 * 这里是 canvas 系列颜色的唯一来源。lightweight-charts 只接受具体色值，
 * 拿不到 CSS 变量，所以调色板定义在 TS 里；需要和画布对齐的 DOM 元素
 * （图例色块等）也从这里取色，而不是在 CSS 里再抄一份。
 */

export interface IndicatorPalette {
  ema9: string
  ema21: string
  volume: string
  volumeLabel: string
  volumeUp: string
  volumeDown: string
  macdDif: string
  macdDea: string
  macdHistogram: string
  macdHistogramUp: string
  macdHistogramDown: string
  kdjK: string
  kdjD: string
  kdjJ: string
}

export interface ChartTheme {
  background: string
  text: string
  axisText: string
  grid: string
  border: string
  /** 阳线 / 阴线 */
  up: string
  down: string
  /** 策略语义价位 */
  signal: string
  filled: string
  pending: string
  average: string
  invalid: string
  /** 首笔成交标记 */
  entryMarker: string
  /** 退出标记：按盈亏取色 */
  exitProfit: string
  exitLoss: string
  /** 策略 schema 扩展位没有指定颜色时的兜底 */
  overlayMarker: string
  overlayLine: string
  /** 权益曲线面积图 */
  areaLine: string
  areaTop: string
  areaBottom: string
  indicators: IndicatorPalette
}

const SHARED_INDICATORS: IndicatorPalette = {
  ema9: '#f5c451',
  ema21: '#66b3ff',
  volume: '#2ebd85',
  volumeLabel: '#7cc9a7',
  volumeUp: '#2ebd8566',
  volumeDown: '#f0525266',
  macdDif: '#4da3ff',
  macdDea: '#f5c451',
  macdHistogram: '#2ebd85',
  macdHistogramUp: '#2ebd8566',
  macdHistogramDown: '#f0525266',
  kdjK: '#4da3ff',
  kdjD: '#f5c451',
  kdjJ: '#d98bff',
}

const LIGHT_THEME: ChartTheme = {
  background: '#ffffff',
  text: '#334155',
  axisText: '#64748b',
  grid: '#e2e8f0',
  border: '#cbd5e1',
  up: '#059669',
  down: '#e11d48',
  signal: '#b45309',
  filled: '#ea580c',
  pending: '#2563eb',
  average: '#1e293b',
  invalid: '#dc2626',
  entryMarker: '#1677ff',
  exitProfit: '#2ebd85',
  exitLoss: '#f05252',
  overlayMarker: '#d6a84b',
  overlayLine: '#8b949e',
  areaLine: '#16a34a',
  areaTop: 'rgba(22, 163, 74, .28)',
  areaBottom: 'rgba(22, 163, 74, .02)',
  indicators: SHARED_INDICATORS,
}

const DARK_THEME: ChartTheme = {
  ...LIGHT_THEME,
  background: '#111827',
  text: '#d7e0ee',
  axisText: '#94a8c1',
  grid: '#263243',
  border: '#41536b',
  signal: '#fbbf24',
  filled: '#fb923c',
  pending: '#60a5fa',
  average: '#e2e8f0',
  invalid: '#f87171',
  up: '#34d399',
  down: '#fb7185',
}

export function getChartTheme(dark: boolean): ChartTheme {
  return dark ? DARK_THEME : LIGHT_THEME
}
