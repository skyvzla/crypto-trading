<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { GripHorizontal } from 'lucide-vue-next'
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type MouseEventParams,
  type SeriesType,
  type SeriesMarker,
  type Time,
  type UTCTimestamp
} from 'lightweight-charts'
import type { BacktestCandle, ChartOverlay } from '@/api/types'
import { formatLedgerDateTime, timestampMs } from '@/shared/time'
import { IS_DARK_THEME } from '@/shared/theme'
import { STORAGE_KEYS, readStored, readStoredRecord, removeStored, writeStored } from '@/shared/storage'
import { getChartTheme, type ChartTheme } from './chartTheme'
import { emaOfClose, kdj, macd } from './indicators'
import type { TradeChartData, TradeChartFillDisplay, TradeChartFillTimeSemantics } from './tradeChart'

interface PriceLineVisibility {
  signal?: boolean
  tiers?: boolean
  average?: boolean
  invalid?: boolean
  extensions?: boolean
}

const props = defineProps<{
  candles: BacktestCandle[]
  trade: TradeChartData
  overlays?: ChartOverlay[]
  focusTime?: string | number | null
  fillDisplay?: TradeChartFillDisplay
  fillTimeSemantics?: TradeChartFillTimeSemantics
  indicators?: {
    volume?: boolean
    macd?: boolean
    ema?: boolean
    kdj?: boolean
  }
  lineVisibility?: PriceLineVisibility
}>()
const emit = defineEmits<{ 'request-more': [direction: 'before' | 'after'] }>()
const isDarkTheme = inject(IS_DARK_THEME, computed(() => false))
const palette = computed<ChartTheme>(() => getChartTheme(isDarkTheme.value))

/** 图表可拖拽高度的边界。 */
const MIN_CHART_HEIGHT = 360
const VIEWPORT_HEIGHT_MARGIN = 80
/** 视窗接近数据边缘时的预取节流窗口，避免重建后立刻重复请求。 */
const EDGE_REQUEST_SUPPRESS_MS = 500
/** 默认展示的前后 K 线根数。 */
const FOCUS_HALF_WINDOW_BARS = 30

const host = ref<HTMLElement | null>(null)
const hoverLabel = ref<{ left: number; top: number; lines: Array<{ label: string; value: string }> } | null>(null)
const indicatorLabels = ref<Array<{ key: string; top: number; values: Array<{ label: string; value: string; color: string }> }>>([])
const extremaLabels = ref<Array<{ left: number; top: number; text: string; color: string }>>([])

type ChartCandle = Omit<BacktestCandle, 'time'> & { time: UTCTimestamp }
type IndicatorApi = ISeriesApi<'Line'> | ISeriesApi<'Histogram'>
interface IndicatorGroup {
  key: 'ema' | 'volume' | 'macd' | 'kdj'
  paneIndex: number
  values: Array<{ label: string; color: string; series: IndicatorApi; format?: 'volume' | 'oscillator' }>
}
type PriceLineSpec = { price: number; title: string; color: string; style: LineStyle; lineWidth: 1 | 2 | 3 | 4; priority: number }

// 图表实例与渲染派生物。这些是命令式绘图库的句柄，不参与 Vue 响应式。
let chart: IChartApi | null = null
let candleSeries: ISeriesApi<'Candlestick'> | null = null
let observer: ResizeObserver | null = null
let unsubscribeRange: (() => void) | null = null
let unsubscribeCrosshair: (() => void) | null = null
let stopHeightResize: (() => void) | null = null
let requestedEdge: 'before' | 'after' | null = null
let renderedCandles: ChartCandle[] = []
let renderedBarTimes: number[] = []
let renderedCandleByTime = new Map<number, ChartCandle>()
let dataUpdaters: Array<(data: ChartCandle[]) => void> = []
let indicatorGroups: IndicatorGroup[] = []
let priceLineHandles: IPriceLine[] = []
let extremaPoints: Array<{ time: UTCTimestamp; price: number; position: 'above' | 'below'; text: string }> = []
let extremaPricePrecision = 2
let suppressEdgeRequestsUntil = 0
let handleVisibleRangeChange: ((range: { from: number; to: number } | null, force?: boolean) => void) | null = null

// 渲染串行化。renderChart 内部有 await，多个 watcher 在同一 tick 触发时会交错：
// 前一次刚 destroy 还没 createChart，后一次就进来了，最后同一个 host 上挂了两张图，
// 而模块级 chart 只指向后一张——前一张连同它的 ResizeObserver 永远收不回来。
let renderChain: Promise<void> = Promise.resolve()
let renderToken = 0
let preserveViewRequested = true
let disposed = false

/**
 * 请求一次重建，同一 tick 内的多次请求合并成最后一次执行。
 *
 * preserveView=false 优先：只要有一个来源要求重新定位（换交易、换数据），
 * 合并后的这次渲染就不保留旧视窗，否则切换交易后会停在上一笔的缩放位置。
 */
function requestRender(preserveView: boolean): Promise<void> {
  preserveViewRequested = preserveViewRequested && preserveView
  const token = ++renderToken
  const next = renderChain.then(async () => {
    if (disposed || token !== renderToken) return
    const preserve = preserveViewRequested
    preserveViewRequested = true
    await renderChart(preserve ? currentVisibleRange() : null)
  })
  renderChain = next.catch(() => undefined)
  return next
}

/** 增量更新会读写 renderedCandles 与现有 series，必须和重建排在同一条队列上。 */
function requestDataUpdate(): Promise<void> {
  const next = renderChain.then(async () => {
    if (disposed) return
    await updateChartData()
  })
  renderChain = next.catch(() => undefined)
  return next
}

function clampChartHeight(value: number): number {
  const ceiling = Math.max(MIN_CHART_HEIGHT, window.innerHeight - VIEWPORT_HEIGHT_MARGIN)
  return Math.max(MIN_CHART_HEIGHT, Math.min(ceiling, value))
}

function storedChartHeight(): number | null {
  const value = Number(readStored(STORAGE_KEYS.chartHeight))
  return Number.isFinite(value) && value > 0 ? clampChartHeight(value) : null
}

const chartHeight = ref<number | null>(storedChartHeight())

function seconds(value: string | number | null | undefined): UTCTimestamp | null {
  const ms = timestampMs(value)
  return ms === null ? null : Math.floor(ms / 1000) as UTCTimestamp
}

function triggerCandleSeconds(value: string | number | null | undefined): UTCTimestamp | null {
  const ms = timestampMs(value)
  // 回测以 1 秒 K 线收齐时作为 fill_time；图表 K 线以开盘时刻为坐标。
  return ms === null ? null : Math.floor((ms - 1_000) / 1_000) as UTCTimestamp
}

function fillCandleSeconds(value: string | number | null | undefined): UTCTimestamp | null {
  return props.fillTimeSemantics === 'exchange' ? seconds(value) : triggerCandleSeconds(value)
}

/**
 * K 线坐标轴与十字光标的时间文案。
 *
 * 必须和表格用同一个账本时区，否则同一笔成交在列表里是上海时间、
 * 在图上却是浏览器本地时间，跨时区看盘时两边对不上。
 */
function formatChartTime(value: number, includeSeconds = true): string {
  return formatLedgerDateTime(value, { seconds: includeSeconds }) ?? '-'
}

function normalizeCandles(candles: BacktestCandle[]): ChartCandle[] {
  return candles
    .map((bar) => ({ ...bar, time: (bar.time > 10_000_000_000 ? Math.floor(bar.time / 1000) : bar.time) as UTCTimestamp }))
    .sort((a, b) => Number(a.time) - Number(b.time))
}

/** 价格轴精度取行情实际小数位，低价币才不会被压成 0.00。 */
function chartPricePrecision(data: ChartCandle[]): number {
  const decimalPlaces = (value: number) => {
    const fixed = Math.abs(value).toFixed(12).replace(/0+$/, '')
    const separator = fixed.indexOf('.')
    return separator === -1 ? 0 : fixed.length - separator - 1
  }
  return Math.min(12, Math.max(2, ...data.flatMap((bar) => [bar.open, bar.high, bar.low, bar.close].map(decimalPlaces))))
}

function lineData(data: ChartCandle[], values: Array<number | null>) {
  return data.flatMap((bar, index) => {
    const value = values[index]
    return value === null || !Number.isFinite(value) ? [] : [{ time: bar.time, value }]
  })
}

function samePrice(left: number, right: number): boolean {
  return Math.abs(left - right) <= Math.max(1e-12, Math.max(Math.abs(left), Math.abs(right)) * 1e-9)
}

// ── 从 trade 派生的成交与档位事实 ─────────────────────────────────────────
// 这些原先内联在渲染函数里，抽成 computed 后标线重绘和标记生成可以共用，
// 也让「哪些是数据推导、哪些是绘图动作」在结构上分开。

const isShort = computed(() => {
  const side = String(props.trade.side || '').toLowerCase()
  return side.includes('short') || side === 'sell'
})
const entrySideLabel = computed(() => isShort.value ? '卖' : '买')
const allFills = computed(() => props.trade.fills || [])
const entryFills = computed(() => {
  const entryOrderSide = isShort.value ? 'sell' : 'buy'
  return allFills.value.filter((fill) => fill.side?.toLowerCase() === entryOrderSide)
})
const displayedFills = computed(() => props.fillDisplay === 'all' ? allFills.value : entryFills.value)
const tierPrices = computed(() => (props.trade.tier_prices || props.trade.orders?.map((item) => item.price) || []).slice(0, 3))

function isLineVisible(key: keyof PriceLineVisibility): boolean {
  return (props.lineVisibility || {})[key] !== false
}

/**
 * 计算当前应该绘制的价格线。
 *
 * 同价位只保留优先级最高的语义（成交价盖过同价限价），因此这里必须
 * 按「信号 → 档位 → 均价 → 失效价 → 策略扩展位」的顺序累加。
 */
function buildPriceLines(): PriceLineSpec[] {
  const colors = palette.value
  const lines: PriceLineSpec[] = []
  const addLine = (
    price: number | null | undefined,
    title: string,
    color: string,
    style = LineStyle.Dashed,
    lineWidth: 1 | 2 | 3 | 4 = 2,
    priority = 0
  ) => {
    if (price === null || price === undefined || !Number.isFinite(price)) return
    const duplicate = lines.findIndex((line) => samePrice(line.price, price))
    const next = { price, title, color, style, lineWidth, priority }
    if (duplicate === -1) lines.push(next)
    else if (lines[duplicate].priority < priority) lines[duplicate] = next
  }

  const tierIsFilled = (price: number, index: number) =>
    entryFills.value.some((fill) => fill.tier === index + 1 || samePrice(fill.price, price))

  if (isLineVisible('signal')) addLine(props.trade.signal_price, '信号', colors.signal, LineStyle.Dashed, 1, 10)
  if (isLineVisible('tiers')) {
    tierPrices.value.forEach((price, index) => {
      const filled = tierIsFilled(price, index)
      addLine(
        price,
        filled ? `${entrySideLabel.value}${index + 1}` : `限${entrySideLabel.value}${index + 1}`,
        filled ? colors.filled : colors.pending,
        filled ? LineStyle.Solid : LineStyle.Dashed,
        1,
        filled ? 50 : 40
      )
    })
  }
  if (isLineVisible('average')) {
    addLine(props.trade.average_entry_price ?? props.trade.entry_price, '开仓均价', colors.average, LineStyle.Solid, 1, 30)
  }
  if (isLineVisible('invalid')) addLine(props.trade.invalid_price, '失效价', colors.invalid, LineStyle.Dotted, 1, 60)

  for (const overlay of props.overlays || []) {
    if (HANDLED_OVERLAY_KEYS.has(overlay.key)) continue
    if (!isLineVisible('extensions') || overlay.kind === 'marker') continue
    const value = overlayNumericValue(overlay.key)
    if (typeof value !== 'number') continue
    const styles = { solid: LineStyle.Solid, dashed: LineStyle.Dashed, dotted: LineStyle.Dotted }
    const style = typeof overlay.line_style === 'number' ? overlay.line_style : styles[overlay.line_style || 'dashed']
    addLine(value, overlay.label || overlay.key, overlay.color || colors.overlayLine, style)
  }
  return lines
}

/** 这些位已由固定语义标线覆盖，策略 schema 再声明一次就会画重复线。 */
const HANDLED_OVERLAY_KEYS = new Set(['spike_high', 'invalid_price', 'tier1_price', 'tier2_price', 'tier3_price'])

function overlayCandidates(key: string): Array<unknown> {
  return [
    props.trade.strategy_data?.[key],
    props.trade.attributes?.[key],
    props.trade.metrics?.[key],
    props.trade.parameters?.[key]
  ]
}

function overlayNumericValue(key: string): number | undefined {
  return overlayCandidates(key).find((item) => typeof item === 'number') as number | undefined
}

/**
 * 就地重绘价格线。
 *
 * 标线显隐是最常被点的控件，重建整张图会丢掉缩放位置，
 * 所以这里只增删价格线句柄。
 */
function applyPriceLines() {
  if (!candleSeries) return
  const series = candleSeries
  priceLineHandles.forEach((line) => series.removePriceLine(line))
  priceLineHandles = buildPriceLines().map(({ price, title, color, style, lineWidth }) =>
    series.createPriceLine({ price, title, color, lineWidth, lineStyle: style, axisLabelVisible: true })
  )
}

// ── 指标窗格 ─────────────────────────────────────────────────────────────

function restorePaneHeights() {
  if (!chart) return
  const stretch = readStoredRecord(STORAGE_KEYS.indicatorPaneStretch)
  const panes = chart.panes()
  indicatorGroups.forEach(({ key, paneIndex }) => {
    const factor = stretch[key]
    if (Number.isFinite(factor) && factor > 0 && panes[paneIndex]) panes[paneIndex].setStretchFactor(factor)
  })
}

function persistPaneHeights() {
  if (!chart) return
  const panes = chart.panes()
  const stretch = readStoredRecord(STORAGE_KEYS.indicatorPaneStretch)
  indicatorGroups.forEach(({ key, paneIndex }) => {
    if (panes[paneIndex]) stretch[key] = panes[paneIndex].getStretchFactor()
  })
  writeStored(STORAGE_KEYS.indicatorPaneStretch, JSON.stringify(stretch))
  refreshIndicatorPositions()
}

function capturePaneResize() {
  window.addEventListener('pointerup', () => window.setTimeout(persistPaneHeights, 0), { once: true })
}

function refreshIndicatorPositions() {
  if (!chart || !host.value || !indicatorLabels.value.length) return
  const hostTop = host.value.getBoundingClientRect().top
  const panes = chart.panes()
  indicatorLabels.value = indicatorLabels.value.map((label) => {
    const group = indicatorGroups.find((item) => item.key === label.key)
    const paneTop = group ? panes[group.paneIndex]?.getHTMLElement()?.getBoundingClientRect().top : null
    return { ...label, top: paneTop == null ? label.top : paneTop - hostTop + 7 }
  })
}

/**
 * 建立指标系列。
 *
 * 每个指标都注册一个 updater，追加 K 线时走增量刷新而不是重建图表。
 * 返回下一个可用窗格号；主图指标（EMA）留在窗格 0。
 */
function setupIndicators(instance: IChartApi, data: ChartCandle[], priceFormat: Record<string, unknown>) {
  const colors = palette.value.indicators
  const settings = props.indicators || {}
  let paneIndex = 1

  if (settings.ema) {
    const ema9 = instance.addSeries(LineSeries, { color: colors.ema9, lineWidth: 1, title: 'EMA9', priceFormat })
    const ema21 = instance.addSeries(LineSeries, { color: colors.ema21, lineWidth: 1, title: 'EMA21', priceFormat })
    const update = (next: ChartCandle[]) => {
      ema9.setData(lineData(next, emaOfClose(next, 9)))
      ema21.setData(lineData(next, emaOfClose(next, 21)))
    }
    update(data)
    dataUpdaters.push(update)
    indicatorGroups.push({
      key: 'ema', paneIndex: 0,
      values: [
        { label: 'EMA9', color: colors.ema9, series: ema9 },
        { label: 'EMA21', color: colors.ema21, series: ema21 }
      ]
    })
  }

  if (settings.volume) {
    const volume = instance.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
      color: colors.volume
    }, paneIndex)
    const update = (next: ChartCandle[]) => volume.setData(next.map((bar) => ({
      time: bar.time,
      value: bar.volume,
      color: bar.close >= bar.open ? colors.volumeUp : colors.volumeDown
    })))
    update(data)
    dataUpdaters.push(update)
    instance.priceScale('volume', paneIndex).applyOptions({ scaleMargins: { top: 0.1, bottom: 0.05 } })
    indicatorGroups.push({ key: 'volume', paneIndex, values: [{ label: 'VOL', color: colors.volumeLabel, series: volume, format: 'volume' }] })
    paneIndex += 1
  }

  if (settings.macd) {
    const dif = instance.addSeries(LineSeries, { color: colors.macdDif, lineWidth: 1, title: 'DIF', priceFormat }, paneIndex)
    const dea = instance.addSeries(LineSeries, { color: colors.macdDea, lineWidth: 1, title: 'DEA', priceFormat }, paneIndex)
    const histogram = instance.addSeries(HistogramSeries, { color: colors.macdHistogram, priceFormat }, paneIndex)
    const update = (next: ChartCandle[]) => {
      const result = macd(next)
      dif.setData(lineData(next, result.dif))
      dea.setData(lineData(next, result.dea))
      histogram.setData(next.map((bar, index) => ({
        time: bar.time,
        value: result.histogram[index],
        color: result.histogram[index] >= 0 ? colors.macdHistogramUp : colors.macdHistogramDown
      })))
    }
    update(data)
    dataUpdaters.push(update)
    indicatorGroups.push({
      key: 'macd', paneIndex,
      values: [
        { label: 'DIF', color: colors.macdDif, series: dif, format: 'oscillator' },
        { label: 'DEA', color: colors.macdDea, series: dea, format: 'oscillator' },
        { label: 'MACD', color: colors.volumeLabel, series: histogram, format: 'oscillator' }
      ]
    })
    paneIndex += 1
  }

  if (settings.kdj) {
    const kSeries = instance.addSeries(LineSeries, { color: colors.kdjK, lineWidth: 1, title: 'K' }, paneIndex)
    const dSeries = instance.addSeries(LineSeries, { color: colors.kdjD, lineWidth: 1, title: 'D' }, paneIndex)
    const jSeries = instance.addSeries(LineSeries, { color: colors.kdjJ, lineWidth: 1, title: 'J' }, paneIndex)
    const update = (next: ChartCandle[]) => {
      const result = kdj(next)
      kSeries.setData(lineData(next, result.k))
      dSeries.setData(lineData(next, result.d))
      jSeries.setData(lineData(next, result.j))
    }
    update(data)
    dataUpdaters.push(update)
    indicatorGroups.push({
      key: 'kdj', paneIndex,
      values: [
        { label: 'K', color: colors.kdjK, series: kSeries, format: 'oscillator' },
        { label: 'D', color: colors.kdjD, series: dSeries, format: 'oscillator' },
        { label: 'J', color: colors.kdjJ, series: jSeries, format: 'oscillator' }
      ]
    })
  }
}

// ── 极值标签 ─────────────────────────────────────────────────────────────

function refreshExtremaLabels() {
  if (!chart || !candleSeries || !host.value) return
  const instance = chart
  const series = candleSeries
  const element = host.value
  const color = palette.value.text
  extremaLabels.value = extremaPoints.flatMap((point) => {
    const left = instance.timeScale().timeToCoordinate(point.time)
    const priceY = series.priceToCoordinate(point.price)
    if (left === null || priceY === null) return []
    return [{
      left: Math.min(Math.max(4, left), element.clientWidth - 4),
      top: point.position === 'above' ? Math.max(2, priceY - 18) : priceY + 4,
      text: point.text,
      color
    }]
  })
}

function updateExtremaPoints() {
  if (!renderedCandles.length) return
  const visibleRange = chart?.timeScale().getVisibleLogicalRange()
  const first = visibleRange ? Math.max(0, Math.floor(visibleRange.from)) : 0
  const last = visibleRange ? Math.min(renderedCandles.length - 1, Math.ceil(visibleRange.to)) : renderedCandles.length - 1
  const visibleCandles = renderedCandles.slice(first, last + 1)
  if (!visibleCandles.length) return
  const highest = visibleCandles.reduce((best, candle) => candle.high > best.high ? candle : best)
  const lowest = visibleCandles.reduce((best, candle) => candle.low < best.low ? candle : best)
  const format = (value: number) => value.toFixed(extremaPricePrecision)
  extremaPoints = [
    { time: highest.time, price: highest.high, position: 'above', text: format(highest.high) },
    { time: lowest.time, price: lowest.low, position: 'below', text: format(lowest.low) }
  ]
}

// ── 高度拖拽 ─────────────────────────────────────────────────────────────

function beginHeightResize(event: PointerEvent) {
  if (event.button !== 0 || !host.value) return
  event.preventDefault()
  const startY = event.clientY
  const startHeight = host.value.getBoundingClientRect().height
  const move = (moveEvent: PointerEvent) => {
    chartHeight.value = clampChartHeight(startHeight + moveEvent.clientY - startY)
  }
  const stop = () => {
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', stop)
    document.body.style.userSelect = ''
    stopHeightResize = null
    if (chartHeight.value !== null) writeStored(STORAGE_KEYS.chartHeight, String(chartHeight.value))
  }
  stopHeightResize?.()
  stopHeightResize = stop
  document.body.style.userSelect = 'none'
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', stop, { once: true })
}

// ── 渲染 ─────────────────────────────────────────────────────────────────

function destroy() {
  observer?.disconnect()
  observer = null
  unsubscribeRange?.()
  unsubscribeRange = null
  unsubscribeCrosshair?.()
  unsubscribeCrosshair = null
  hoverLabel.value = null
  indicatorLabels.value = []
  extremaLabels.value = []
  requestedEdge = null
  renderedCandles = []
  renderedBarTimes = []
  renderedCandleByTime = new Map()
  dataUpdaters = []
  indicatorGroups = []
  priceLineHandles = []
  candleSeries = null
  extremaPoints = []
  extremaPricePrecision = 2
  handleVisibleRangeChange = null
  chart?.remove()
  chart = null
}

/** 把精确时刻吸附到它所落在的那根 K 线的开盘时间。 */
function barTimeAt(value: string | number | null | undefined): UTCTimestamp | null {
  const exact = seconds(value)
  if (exact === null || !renderedBarTimes.length) return null
  const intervalSeconds = renderedBarTimes.length > 1 ? renderedBarTimes[1] - renderedBarTimes[0] : 60
  if (exact < renderedBarTimes[0] || exact > renderedBarTimes[renderedBarTimes.length - 1] + intervalSeconds) return null
  let low = 0
  let high = renderedBarTimes.length - 1
  let matched = renderedBarTimes[0]
  while (low <= high) {
    const middle = Math.floor((low + high) / 2)
    if (renderedBarTimes[middle] <= exact) {
      matched = renderedBarTimes[middle]
      low = middle + 1
    } else {
      high = middle - 1
    }
  }
  return matched as UTCTimestamp
}

function fillBarTime(value: string | number | null | undefined): UTCTimestamp | null {
  const triggerTime = fillCandleSeconds(value)
  return triggerTime === null ? null : barTimeAt(triggerTime * 1_000)
}

type EventPrice = { label: string; price: number; priority: number }

/**
 * 十字光标浮层。
 *
 * 悬停时展示该根 K 线的量价信息、落在这根 K 线上的策略事件价格，
 * 以及各指标窗格的当期值。抽成独立工厂是为了让 renderChart 只负责编排。
 */
function createCrosshairHandler(context: {
  series: ISeriesApi<'Candlestick'>
  eventPrices: Map<number, EventPrice[]>
  pricePrecision: number
  candleSpacing: number
}) {
  const { series, eventPrices, pricePrecision, candleSpacing } = context
  const formatPrice = (value: number | undefined) => value == null ? '-' : Number(value).toFixed(pricePrecision)
  const compact = (value: number) =>
    new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 2 }).format(value)
  const formatIndicatorValue = (value: number, format?: 'volume' | 'oscillator') => {
    if (format === 'volume') return compact(value)
    if (format === 'oscillator') return Number(value).toFixed(4)
    return formatPrice(value)
  }

  return (param: MouseEventParams<Time>) => {
    if (!param.point || param.time === undefined) {
      hoverLabel.value = null
      indicatorLabels.value = []
      return
    }
    const candle = param.seriesData.get(series) as { open?: number; high?: number; low?: number; close?: number } | undefined
    if (!candle || !host.value) return
    const element = host.value
    const sourceCandle = renderedCandleByTime.get(Number(param.time))
    const events = eventPrices.get(Number(param.time)) || []
    const open = candle.open || 0
    const change = (candle.close ?? 0) - open
    const changePercent = open === 0 ? 0 : change / open * 100
    const amplitude = open === 0 ? 0 : ((candle.high ?? 0) - (candle.low ?? 0)) / open * 100
    const lines = [
      { label: '时间', value: formatChartTime(Number(param.time) * 1000, candleSpacing < 60) },
      { label: '开', value: formatPrice(candle.open) },
      { label: '高', value: formatPrice(candle.high) },
      { label: '低', value: formatPrice(candle.low) },
      { label: '收', value: formatPrice(candle.close) },
      { label: '涨跌', value: formatPrice(change) },
      { label: '涨跌幅', value: `${changePercent.toFixed(2)}%` },
      { label: '振幅', value: `${amplitude.toFixed(2)}%` },
      { label: '成交量', value: sourceCandle == null ? '-' : compact(sourceCandle.volume) },
      ...events.map((event) => ({ label: event.label, value: formatPrice(event.price) }))
    ]
    hoverLabel.value = {
      left: Math.min(Math.max(8, param.point.x + 12), Math.max(8, element.clientWidth - 210)),
      top: Math.min(Math.max(8, param.point.y + 12), Math.max(8, element.clientHeight - lines.length * 23 - 16)),
      lines
    }
    const hostTop = element.getBoundingClientRect().top
    const panes = chart?.panes() || []
    indicatorLabels.value = indicatorGroups.flatMap((group) => {
      const values = group.values.flatMap((item) => {
        const point = param.seriesData.get(item.series as ISeriesApi<SeriesType, Time>) as { value?: number } | undefined
        return typeof point?.value === 'number'
          ? [{ label: item.label, value: formatIndicatorValue(point.value, item.format), color: item.color }]
          : []
      })
      const paneTop = panes[group.paneIndex]?.getHTMLElement()?.getBoundingClientRect().top
      return values.length ? [{ key: group.key, top: paneTop == null ? 7 : paneTop - hostTop + 7, values }] : []
    })
  }
}

async function renderChart(preservedRange: { from: Time; to: Time } | null = null) {
  destroy()
  await nextTick()
  if (disposed || !host.value || !props.candles.length) return
  const colors = palette.value
  const data = normalizeCandles(props.candles)
  const pricePrecision = chartPricePrecision(data)
  const priceFormat = { type: 'price' as const, precision: pricePrecision, minMove: 10 ** -pricePrecision }
  const candleSpacing = data.length > 1 ? Number(data[1].time) - Number(data[0].time) : 60

  chart = createChart(host.value, {
    width: host.value.clientWidth,
    height: Math.max(420, host.value.clientHeight),
    layout: {
      background: { type: ColorType.Solid, color: colors.background }, textColor: colors.text, attributionLogo: true,
      panes: { enableResize: true, separatorColor: colors.grid, separatorHoverColor: colors.border }
    },
    grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
    rightPriceScale: { borderColor: colors.border },
    timeScale: { borderColor: colors.border, timeVisible: true, secondsVisible: candleSpacing < 60 },
    crosshair: { mode: CrosshairMode.Normal, vertLine: { color: colors.border }, horzLine: { color: colors.border } },
    localization: {
      locale: 'zh-CN',
      timeFormatter: (time: Time) => {
        if (typeof time === 'number') return formatChartTime(time * 1000, candleSpacing < 60)
        if (typeof time === 'string') return time
        return `${time.year}-${String(time.month).padStart(2, '0')}-${String(time.day).padStart(2, '0')}`
      }
    }
  })

  const series = chart.addSeries(CandlestickSeries, {
    upColor: colors.up, downColor: colors.down, borderVisible: false,
    wickUpColor: colors.up, wickDownColor: colors.down, priceFormat
  })
  candleSeries = series
  renderedCandles = data
  renderedBarTimes = data.map((bar) => Number(bar.time))
  renderedCandleByTime = new Map(data.map((bar) => [Number(bar.time), bar]))
  extremaPricePrecision = pricePrecision
  series.setData(data)
  dataUpdaters.push((nextData) => series.setData(nextData))

  setupIndicators(chart, data, priceFormat)

  const overlayMarkers: SeriesMarker<UTCTimestamp>[] = []
  for (const overlay of props.overlays || []) {
    if (HANDLED_OVERLAY_KEYS.has(overlay.key) || overlay.kind !== 'marker') continue
    const raw = overlayCandidates(overlay.key).find((item) => typeof item === 'object' && item !== null && !Array.isArray(item))
    const record = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw as Record<string, unknown> : null
    const time = barTimeAt(record?.time as string | number | undefined)
    if (time) {
      overlayMarkers.push({ time, position: 'aboveBar', color: overlay.color || colors.overlayMarker, shape: 'circle', text: overlay.label || overlay.key })
    }
  }

  const entryPosition = isShort.value ? 'aboveBar' : 'belowBar'
  const exitPosition = isShort.value ? 'belowBar' : 'aboveBar'
  const entryShape = isShort.value ? 'arrowDown' : 'arrowUp'
  const exitShape = isShort.value ? 'arrowUp' : 'arrowDown'

  const tierForFill = (price: number, fallbackIndex: number) => {
    const index = tierPrices.value.findIndex((tierPrice) => samePrice(tierPrice, price))
    return index >= 0 ? index + 1 : fallbackIndex + 1
  }
  const fillCounts = new Map<'buy' | 'sell', number>()
  const displayedFillDetails = displayedFills.value.map((fill, index) => {
    const fillSide = String(fill.side || '').toLowerCase()
    const isBuy = fillSide === 'buy' || fillSide.includes('long')
    const sideKey = isBuy ? 'buy' : 'sell'
    const count = (fillCounts.get(sideKey) || 0) + 1
    fillCounts.set(sideKey, count)
    const showAll = props.fillDisplay === 'all'
    return {
      fill,
      label: showAll ? `${isBuy ? '买' : '卖'}${count}` : `${entrySideLabel.value}${tierForFill(fill.price, index)}`,
      position: showAll ? (isBuy ? 'belowBar' as const : 'aboveBar' as const) : (entryPosition as 'aboveBar' | 'belowBar'),
      shape: showAll ? (isBuy ? 'arrowUp' as const : 'arrowDown' as const) : (entryShape as 'arrowUp' | 'arrowDown'),
      color: showAll ? (isBuy ? colors.filled : colors.invalid) : colors.filled
    }
  })

  const markers: SeriesMarker<UTCTimestamp>[] = []
  const signalTime = barTimeAt(props.trade.signal_time)
  if (signalTime) markers.push({ time: signalTime, position: entryPosition, color: colors.signal, shape: 'circle', text: '信号' })
  for (const detail of displayedFillDetails) {
    const time = fillBarTime(detail.fill.time)
    if (time) markers.push({ time, position: detail.position, color: detail.color, shape: detail.shape, text: detail.label })
  }
  const firstFill = displayedFills.value[0] ?? entryFills.value[0]
  const entryTime = fillBarTime(firstFill?.time ?? props.trade.entry_time)
  if (entryTime && !markers.some((marker) => Number(marker.time) === Number(entryTime))) {
    markers.push({ time: entryTime, position: entryPosition, color: colors.entryMarker, shape: entryShape, text: '首单' })
  }
  const exitTime = fillBarTime(props.trade.exit_time)
  if (props.fillDisplay !== 'all' && exitTime) {
    markers.push({
      time: exitTime, position: exitPosition,
      color: Number(props.trade.net_pnl || 0) >= 0 ? colors.exitProfit : colors.exitLoss,
      shape: exitShape, text: '退出'
    })
  }

  applyPriceLines()
  createSeriesMarkers(series, [...markers, ...overlayMarkers].sort((a, b) => Number(a.time) - Number(b.time)))

  // 十字光标要显示落在该 K 线上的策略事件价格，先按 K 线时间归组。
  const eventPrices = new Map<number, EventPrice[]>()
  const addEventPrice = (label: string, value: string | number | null | undefined, price: number | null | undefined, isFill = false, priority = 0) => {
    const time = isFill ? fillBarTime(value) : barTimeAt(value)
    if (time === null || typeof price !== 'number') return
    const current = eventPrices.get(Number(time)) || []
    const samePriceIndex = current.findIndex((item) => samePrice(item.price, price))
    if (samePriceIndex === -1) current.push({ label, price, priority })
    else if (current[samePriceIndex].priority < priority) current[samePriceIndex] = { label, price, priority }
    eventPrices.set(Number(time), current)
  }
  addEventPrice('信号价格', props.trade.signal_time, props.trade.signal_price, false, 10)
  displayedFillDetails.forEach(({ fill, label }) => addEventPrice(label, fill.time, fill.price, true, 50))
  addEventPrice('开仓均价', props.trade.entry_time, props.trade.average_entry_price ?? props.trade.entry_price, true, 30)
  if (props.fillDisplay !== 'all') addEventPrice('退出价格', props.trade.exit_time, props.trade.exit_price, true, 50)

  const handleCrosshair = createCrosshairHandler({ series, eventPrices, pricePrecision, candleSpacing })

  const initialFocusTime = Number(
    props.focusTime == null
      ? fillCandleSeconds(firstFill?.time ?? props.trade.entry_time) ?? seconds(props.trade.signal_time)
      : seconds(props.focusTime) ?? renderedBarTimes[Math.floor(renderedBarTimes.length / 2)]
  )
  let focusIndex = renderedBarTimes.findIndex((time) => time >= initialFocusTime)
  if (focusIndex < 0) focusIndex = renderedBarTimes.length - 1
  const timeScale = chart.timeScale()
  chart.subscribeCrosshairMove(handleCrosshair)
  unsubscribeCrosshair = () => chart?.unsubscribeCrosshairMove(handleCrosshair)

  const requestMore = (range: { from: number; to: number } | null, force = false) => {
    updateExtremaPoints()
    refreshExtremaLabels()
    if (!range || (!force && Date.now() < suppressEdgeRequestsUntil)) return
    const visibleBars = Math.max(1, range.to - range.from)
    const prefetchBars = Math.max(20, Math.ceil(visibleBars * 3))
    const nearStart = range.from <= prefetchBars
    const nearEnd = range.to >= renderedBarTimes.length - 1 - prefetchBars
    // 缩得足够小时两侧会同时进入预取区。此时按离数据边界的距离选择，
    // 否则固定优先 before 会让右侧后续 K 线永远得不到加载机会。
    const edge = nearStart && nearEnd
      ? (range.from < renderedBarTimes.length - 1 - range.to ? 'before' : 'after')
      : nearStart ? 'before' : nearEnd ? 'after' : null
    if (edge && edge !== requestedEdge) {
      requestedEdge = edge
      emit('request-more', edge)
    } else if (!edge) {
      requestedEdge = null
    }
  }
  handleVisibleRangeChange = requestMore
  suppressEdgeRequestsUntil = Date.now() + EDGE_REQUEST_SUPPRESS_MS
  if (preservedRange) timeScale.setVisibleRange(preservedRange)
  else timeScale.setVisibleLogicalRange({
    from: Math.max(0, focusIndex - FOCUS_HALF_WINDOW_BARS),
    to: Math.min(renderedBarTimes.length - 1, focusIndex + FOCUS_HALF_WINDOW_BARS)
  })
  timeScale.subscribeVisibleLogicalRangeChange(requestMore)
  unsubscribeRange = () => timeScale.unsubscribeVisibleLogicalRangeChange(requestMore)

  await nextTick()
  // 这次 await 期间可能已经卸载，onBeforeUnmount 的 destroy 已经收掉上面建的图，
  // 这里必须停下，不能再往一个已经脱离文档的 host 上挂 observer。
  if (disposed || !host.value) return
  restorePaneHeights()
  updateExtremaPoints()
  refreshExtremaLabels()
  requestAnimationFrame(() => {
    updateExtremaPoints()
    refreshExtremaLabels()
  })

  observer = new ResizeObserver((entries) => {
    const { width, height } = entries[0]?.contentRect || {}
    if (width && height && chart) {
      chart.applyOptions({ width, height })
      refreshIndicatorPositions()
      refreshExtremaLabels()
    }
  })
  observer.observe(host.value)
}

/** 当前视窗范围，用于「重建但不丢失用户缩放位置」。 */
function currentVisibleRange(): { from: Time; to: Time } | null {
  return chart?.timeScale().getVisibleRange() ?? null
}

/** 把某个已加载的时刻居中显示；时刻不在已加载区间内时什么都不做。 */
function centerOn(timeSeconds: number | null) {
  if (timeSeconds === null || !chart || !renderedBarTimes.length) return
  if (timeSeconds < renderedBarTimes[0] || timeSeconds > renderedBarTimes[renderedBarTimes.length - 1]) return
  let index = renderedBarTimes.findIndex((item) => item >= timeSeconds)
  if (index < 0) index = renderedBarTimes.length - 1
  suppressEdgeRequestsUntil = Date.now() + EDGE_REQUEST_SUPPRESS_MS
  chart.timeScale().setVisibleLogicalRange({
    from: Math.max(0, index - FOCUS_HALF_WINDOW_BARS),
    to: Math.min(renderedBarTimes.length - 1, index + FOCUS_HALF_WINDOW_BARS)
  })
}

function focusEvent(value: string | number | null | undefined) {
  const time = fillCandleSeconds(value)
  centerOn(time === null ? null : Number(time))
}

function focusEntry() { focusEvent(allFills.value[0]?.time ?? props.trade.entry_time) }
function focusExit() { focusEvent(props.trade.exit_time) }

async function resetSize() {
  chartHeight.value = null
  removeStored(STORAGE_KEYS.chartHeight)
  removeStored(STORAGE_KEYS.indicatorPaneStretch)
  await requestRender(true)
}
defineExpose({ focusEntry, focusExit, resetSize })

function containsTime(data: ChartCandle[], value: string | number | null | undefined): boolean {
  const time = seconds(value)
  if (time === null || !data.length) return false
  return Number(time) >= Number(data[0].time) && Number(time) <= Number(data[data.length - 1].time)
}

/**
 * 追加 K 线时走增量更新。
 *
 * 只有当聚焦点或某个标记所在的 K 线首次进入数据范围时才必须重建
 * （否则那些标记画不出来）；其余情况保留现有 series 和缩放位置。
 */
async function updateChartData() {
  if (!chart || !dataUpdaters.length || !props.candles.length) {
    await renderChart()
    return
  }
  const nextData = normalizeCandles(props.candles)
  const visibleRange = chart.timeScale().getVisibleRange()
  const previousFirst = Number(renderedCandles[0]?.time)
  const previousLast = Number(renderedCandles[renderedCandles.length - 1]?.time)
  const nextFirst = Number(nextData[0]?.time)
  const nextLast = Number(nextData[nextData.length - 1]?.time)
  const requestedEdgeFilled = requestedEdge === 'before'
    ? nextFirst < previousFirst
    : requestedEdge === 'after' && nextLast > previousLast
  const focusArrived = !containsTime(renderedCandles, props.focusTime) && containsTime(nextData, props.focusTime)
  const eventTimes = [
    props.trade.signal_time,
    fillCandleSeconds(props.trade.entry_time),
    fillCandleSeconds(props.trade.exit_time),
    ...allFills.value.map((fill) => fillCandleSeconds(fill.time))
  ]
  const markerArrived = eventTimes.some((time) => !containsTime(renderedCandles, time) && containsTime(nextData, time))
  if (focusArrived || markerArrived) {
    await renderChart(focusArrived ? null : visibleRange)
    if (requestedEdgeFilled && chart) {
      handleVisibleRangeChange?.(chart.timeScale().getVisibleLogicalRange(), true)
    }
    return
  }
  renderedCandles = nextData
  renderedBarTimes = nextData.map((bar) => Number(bar.time))
  renderedCandleByTime = new Map(nextData.map((bar) => [Number(bar.time), bar]))
  dataUpdaters.forEach((update) => update(nextData))
  updateExtremaPoints()
  refreshExtremaLabels()
  if (requestedEdgeFilled) requestedEdge = null
  if (visibleRange) chart.timeScale().setVisibleRange(visibleRange)
  if (requestedEdgeFilled) {
    handleVisibleRangeChange?.(chart.timeScale().getVisibleLogicalRange())
  }
}

watch(() => props.candles, () => void requestDataUpdate(), { deep: true })

// 换了一笔交易或一套 schema 标注 = 换了内容，重新以事件为中心渲染。
watch(() => [props.trade, props.overlays], () => void requestRender(false), { immediate: true, deep: true })

// 指标增删要重排窗格，只能重建；但保留视窗，避免勾一个指标就丢失缩放位置。
watch(() => props.indicators, () => void requestRender(true), { deep: true })

// 标线显隐只影响价格线，就地增删，不动图表。
watch(() => props.lineVisibility, () => applyPriceLines(), { deep: true })

// 主题决定所有 series 颜色，只能重建，同样保留视窗。
watch(isDarkTheme, () => void requestRender(true))

onBeforeUnmount(() => {
  // 先置位再销毁：队列里排着的渲染看到它就不会再把图建回来。
  disposed = true
  stopHeightResize?.()
  document.body.style.userSelect = ''
  destroy()
})
</script>

<template>
  <div class="chart-shell" :style="chartHeight ? { height: `${chartHeight}px` } : undefined">
    <div ref="host" class="candlestick-host" @pointerdown.capture="capturePaneResize">
      <span v-for="label in extremaLabels" :key="`${label.left}-${label.top}-${label.text}`" class="extrema-price-label" :style="{ left: `${label.left}px`, top: `${label.top}px`, color: label.color }">{{ label.text }}</span>
      <div v-if="hoverLabel" class="chart-hover-label" :style="{ left: `${hoverLabel.left}px`, top: `${hoverLabel.top}px` }">
        <div v-for="line in hoverLabel.lines" :key="line.label" class="hover-row"><span>{{ line.label }}</span><strong>{{ line.value }}</strong></div>
      </div>
      <div v-for="label in indicatorLabels" :key="label.key" class="indicator-hover-label" :style="{ top: `${label.top}px` }">
        <span v-for="item in label.values" :key="item.label" :style="{ color: item.color }">{{ item.label }} {{ item.value }}</span>
      </div>
    </div>
    <div class="chart-height-resizer" role="separator" aria-label="拖动调整图表高度" aria-orientation="horizontal" @pointerdown="beginHeightResize">
      <GripHorizontal :size="16" />
    </div>
  </div>
</template>

<style scoped>
.chart-shell { position: relative; }
.candlestick-host { position: relative; width: 100%; height: calc(100% - 8px); }
.extrema-price-label { position: absolute; z-index: 5; font: var(--font-size-xs)/1.2 var(--font-family-mono); pointer-events: none; transform: translateX(-50%); white-space: nowrap; }
.chart-hover-label {
  position: absolute;
  z-index: 2;
  width: 194px;
  max-width: calc(100% - 16px);
  padding: 6px 9px;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--chart-overlay);
  color: var(--text);
  font: var(--font-size-xs)/1.45 var(--font-family-mono);
  pointer-events: none;
}
.hover-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 19px; white-space: nowrap; }
.hover-row span { color: var(--muted); }
.hover-row strong { color: var(--text); font-weight: 500; }
.indicator-hover-label {
  position: absolute;
  z-index: 2;
  left: 9px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  padding: 3px 6px;
  border-radius: 3px;
  border: 1px solid var(--line);
  background: var(--chart-overlay);
  font: var(--font-size-xs)/1.3 var(--font-family-mono);
  pointer-events: none;
}
.chart-height-resizer {
  position: absolute;
  z-index: 4;
  right: 0;
  bottom: 0;
  left: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 8px;
  color: var(--muted);
  background: var(--surface-hover);
  cursor: ns-resize;
  touch-action: none;
}
.chart-height-resizer:hover {
  color: var(--text);
  background: var(--surface-raised);
}
</style>
