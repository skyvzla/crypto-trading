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
  paneSeparator: string
  paneSeparatorHover: string
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

/**
 * 浅色画布是纯白 `#ffffff`，所有数据标记（非文字图形）都要满足 WCAG 2.x
 * 1.4.11 的 3:1 最低对比度；文字类的 `text` / `axisText` 另有 4.5:1 要求。
 *
 * 下面这几个值是从浅色族里按对比度重新选的，不要直接照搬深色主题——
 * 深色主题的浅底绿 `#2ebd85` 在深色画布上是 7.38:1，但在白底只有 2.40:1。
 *
 * 半透明的成交量 / 柱状图填充同样是数据标记：40% 透明度叠加在白底上，
 * 亮度最高的合成结果也只有 2.85:1（任何颜色都不例外），所以浅色主题
 * 把透明度提高到 75% 并换用更深的基色，合成后仍有 3.3:1 以上。
 */
const LIGHT_INDICATORS: IndicatorPalette = {
  ema9: '#b8860b',
  ema21: '#1d6fb8',
  volume: '#0d9488',
  volumeLabel: '#2f9d72',
  volumeUp: '#0f766ebf',
  volumeDown: '#e11d48bf',
  macdDif: '#1d6fb8',
  macdDea: '#b8860b',
  macdHistogram: '#0d9488',
  macdHistogramUp: '#0f766ebf',
  macdHistogramDown: '#e11d48bf',
  kdjK: '#1d6fb8',
  kdjD: '#b8860b',
  kdjJ: '#7c3aed',
}

const DARK_INDICATORS: IndicatorPalette = {
  ema9: '#f5c451',
  ema21: '#66b3ff',
  volume: '#2ebd85',
  volumeLabel: '#7cc9a7',
  // 与浅色主题同理：40% 透明度叠加在 #111827 上只有 2.21:1 / 1.75:1，
  // 填充也是数据标记，所以两个主题都提到 75%。深色底不需要换基色。
  volumeUp: '#2ebd85bf',
  volumeDown: '#f05252bf',
  macdDif: '#4da3ff',
  macdDea: '#f5c451',
  macdHistogram: '#2ebd85',
  macdHistogramUp: '#2ebd85bf',
  macdHistogramDown: '#f05252bf',
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
  /** 可拖拽的分隔条属于控件边界，1.4.11 同样要求 3:1，故比 grid 深。 */
  paneSeparator: '#8593a5',
  paneSeparatorHover: '#475569',
  up: '#059669',
  down: '#e11d48',
  signal: '#b45309',
  filled: '#ea580c',
  pending: '#2563eb',
  average: '#1e293b',
  invalid: '#dc2626',
  entryMarker: '#1677ff',
  exitProfit: '#0d9488',
  exitLoss: '#f05252',
  overlayMarker: '#a16207',
  overlayLine: '#8b949e',
  areaLine: '#16a34a',
  areaTop: 'rgba(22, 163, 74, .28)',
  areaBottom: 'rgba(22, 163, 74, .02)',
  indicators: LIGHT_INDICATORS,
}

const DARK_THEME: ChartTheme = {
  ...LIGHT_THEME,
  background: '#111827',
  // 这两个值只为浅色画布改深了，深色画布必须显式保留原值：
  // 照搬浅色值会让深色主题的标记明显变差（overlayMarker 8.08:1 → 3.60:1，
  // exitProfit 7.38:1 → 4.74:1）。
  exitProfit: '#2ebd85',
  overlayMarker: '#d6a84b',
  text: '#d7e0ee',
  axisText: '#94a8c1',
  grid: '#263243',
  border: '#41536b',
  paneSeparator: '#64748b',
  paneSeparatorHover: '#94a3b8',
  signal: '#fbbf24',
  filled: '#fb923c',
  pending: '#60a5fa',
  average: '#e2e8f0',
  invalid: '#f87171',
  up: '#34d399',
  down: '#fb7185',
  indicators: DARK_INDICATORS,
}

export function getChartTheme(dark: boolean): ChartTheme {
  return dark ? DARK_THEME : LIGHT_THEME
}
