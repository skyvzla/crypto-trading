<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, ref, watch, type ComputedRef } from 'vue'
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
  type ISeriesApi,
  type MouseEventParams,
  type SeriesType,
  type SeriesMarker,
  type Time,
  type UTCTimestamp
} from 'lightweight-charts'
import type { BacktestCandle, ChartOverlay } from '@/api/types'
import { timestampMs } from './format'
import { getChartTheme } from './chartTheme'
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
const isDarkTheme = inject<ComputedRef<boolean>>('isDarkTheme', computed(() => false))

const host = ref<HTMLElement | null>(null)
let chart: IChartApi | null = null
let observer: ResizeObserver | null = null
let unsubscribeRange: (() => void) | null = null
let unsubscribeCrosshair: (() => void) | null = null
let requestedEdge: 'before' | 'after' | null = null
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
let renderedCandles: ChartCandle[] = []
let renderedBarTimes: number[] = []
let renderedCandleByTime = new Map<number, ChartCandle>()
let dataUpdaters: Array<(data: ChartCandle[]) => void> = []
let indicatorGroups: IndicatorGroup[] = []
let suppressEdgeRequestsUntil = 0
let stopHeightResize: (() => void) | null = null
let candleSeries: ISeriesApi<'Candlestick'> | null = null
let extremaPoints: Array<{ time: UTCTimestamp; price: number; position: 'above' | 'below'; text: string; color: string }> = []
let extremaPricePrecision = 2
let extremaTextColor = '#334155'
const PANE_STRETCH_KEY = 'backtest-replay-indicator-pane-stretch-v1'
const CHART_HEIGHT_KEY = 'backtest-replay-chart-height-v1'

function clampChartHeight(value: number): number {
  return Math.max(360, Math.min(Math.max(360, window.innerHeight - 80), value))
}

function storedChartHeight(): number | null {
  try {
    const value = Number(localStorage.getItem(CHART_HEIGHT_KEY))
    return Number.isFinite(value) && value > 0 ? clampChartHeight(value) : null
  } catch {
    return null
  }
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

function formatChartTime(value: number, includeSeconds = true): string {
  const date = new Date(value)
  const pad = (part: number) => String(part).padStart(2, '0')
  const datePart = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
  const timePart = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  return `${datePart} ${timePart}${includeSeconds ? `:${pad(date.getSeconds())}` : ''}`
}

function normalizeCandles(candles: BacktestCandle[]): ChartCandle[] {
  return candles
    .map((bar) => ({ ...bar, time: (bar.time > 10_000_000_000 ? Math.floor(bar.time / 1000) : bar.time) as UTCTimestamp }))
    .sort((a, b) => Number(a.time) - Number(b.time))
}

function chartPricePrecision(data: ChartCandle[]): number {
  const decimalPlaces = (value: number) => {
    const fixed = Math.abs(value).toFixed(12).replace(/0+$/, '')
    const separator = fixed.indexOf('.')
    return separator === -1 ? 0 : fixed.length - separator - 1
  }
  return Math.min(12, Math.max(2, ...data.flatMap((bar) => [bar.open, bar.high, bar.low, bar.close].map(decimalPlaces))))
}

function emaValues(data: ChartCandle[], period: number): number[] {
  const multiplier = 2 / (period + 1)
  let previous: number | null = null
  return data.map(({ close }) => {
    previous = previous === null ? close : (close - previous) * multiplier + previous
    return previous
  })
}

function lineData(data: ChartCandle[], values: Array<number | null>) {
  return data.flatMap((bar, index) => {
    const value = values[index]
    return value === null || !Number.isFinite(value) ? [] : [{ time: bar.time, value }]
  })
}

function storedPaneStretch(): Record<string, number> {
  try {
    const value = JSON.parse(localStorage.getItem(PANE_STRETCH_KEY) || '{}')
    return value && typeof value === 'object' ? value : {}
  } catch {
    return {}
  }
}

function restorePaneHeights() {
  if (!chart) return
  const stretch = storedPaneStretch()
  const panes = chart.panes()
  indicatorGroups.forEach(({ key, paneIndex }) => {
    const factor = stretch[key]
    if (Number.isFinite(factor) && factor > 0 && panes[paneIndex]) panes[paneIndex].setStretchFactor(factor)
  })
}

function persistPaneHeights() {
  if (!chart) return
  const panes = chart.panes()
  const stretch = storedPaneStretch()
  indicatorGroups.forEach(({ key, paneIndex }) => {
    if (panes[paneIndex]) stretch[key] = panes[paneIndex].getStretchFactor()
  })
  try {
    localStorage.setItem(PANE_STRETCH_KEY, JSON.stringify(stretch))
  } catch {
    // 浏览器禁用本地存储时仍保留本次会话的拖拽结果。
  }
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

function refreshExtremaLabels() {
  if (!chart || !candleSeries || !host.value) return
  extremaLabels.value = extremaPoints.flatMap((point) => {
    const left = chart!.timeScale().timeToCoordinate(point.time)
    const priceY = candleSeries!.priceToCoordinate(point.price)
    if (left === null || priceY === null) return []
    return [{
      left: Math.min(Math.max(4, left), host.value!.clientWidth - 4),
      top: point.position === 'above' ? Math.max(2, priceY - 18) : priceY + 4,
      text: point.text,
      color: point.color
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
    { time: highest.time, price: highest.high, position: 'above', text: format(highest.high), color: extremaTextColor },
    { time: lowest.time, price: lowest.low, position: 'below', text: format(lowest.low), color: extremaTextColor }
  ]
}

function beginHeightResize(event: PointerEvent) {
  if (event.button !== 0 || !host.value) return
  event.preventDefault()
  const startY = event.clientY
  const startHeight = host.value.getBoundingClientRect().height
  const move = (moveEvent: PointerEvent) => {
    chartHeight.value = Math.max(360, Math.min(window.innerHeight - 80, startHeight + moveEvent.clientY - startY))
  }
  const stop = () => {
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', stop)
    document.body.style.userSelect = ''
    stopHeightResize = null
    if (chartHeight.value !== null) {
      try {
        localStorage.setItem(CHART_HEIGHT_KEY, String(chartHeight.value))
      } catch {
        // 浏览器禁用本地存储时仍保留本次会话的拖拽结果。
      }
    }
  }
  stopHeightResize?.()
  stopHeightResize = stop
  document.body.style.userSelect = 'none'
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', stop, { once: true })
}

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
  candleSeries = null
  extremaPoints = []
  extremaPricePrecision = 2
  chart?.remove()
  chart = null
}

async function renderChart(preservedRange: { from: Time; to: Time } | null = null) {
  destroy()
  await nextTick()
  if (!host.value || !props.candles.length) return
  const data = normalizeCandles(props.candles)
  const pricePrecision = chartPricePrecision(data)
  const priceFormat = { type: 'price' as const, precision: pricePrecision, minMove: 10 ** -pricePrecision }
  const candleSpacing = data.length > 1 ? Number(data[1].time) - Number(data[0].time) : 60
  const palette = getChartTheme(isDarkTheme.value)
  chart = createChart(host.value, {
    width: host.value.clientWidth,
    height: Math.max(420, host.value.clientHeight),
    layout: {
      background: { type: ColorType.Solid, color: palette.background }, textColor: palette.text, attributionLogo: true,
      panes: { enableResize: true, separatorColor: palette.grid, separatorHoverColor: palette.border }
    },
    grid: { vertLines: { color: palette.grid }, horzLines: { color: palette.grid } },
    rightPriceScale: { borderColor: palette.border },
    timeScale: { borderColor: palette.border, timeVisible: true, secondsVisible: candleSpacing < 60 },
    crosshair: { mode: CrosshairMode.Normal, vertLine: { color: palette.border }, horzLine: { color: palette.border } },
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
    upColor: palette.up, downColor: palette.down, borderVisible: false,
    wickUpColor: palette.up, wickDownColor: palette.down, priceFormat
  })
  candleSeries = series
  renderedCandles = data
  renderedBarTimes = data.map((bar) => Number(bar.time))
  renderedCandleByTime = new Map(data.map((bar) => [Number(bar.time), bar]))
  series.setData(data)
  dataUpdaters.push((nextData) => series.setData(nextData))
  const markerTime = (value: string | number | null | undefined): UTCTimestamp | null => {
    const exact = seconds(value)
    if (exact === null || !renderedBarTimes.length) return null
    const intervalSeconds = renderedBarTimes.length > 1
      ? renderedBarTimes[1] - renderedBarTimes[0]
      : 60
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
  const fillMarkerTime = (value: string | number | null | undefined): UTCTimestamp | null => {
    const triggerTime = fillCandleSeconds(value)
    return triggerTime === null ? null : markerTime(triggerTime * 1_000)
  }

  const isShort = String(props.trade.side || '').toLowerCase().includes('short') || String(props.trade.side || '').toLowerCase() === 'sell'
  const entrySideLabel = isShort ? '卖' : '买'
  const entryOrderSide = isShort ? 'sell' : 'buy'
  const allFills = props.trade.fills || []
  const entryFills = allFills.filter((fill) => fill.side?.toLowerCase() === entryOrderSide)
  const displayedFills = props.fillDisplay === 'all' ? allFills : entryFills
  const tierPrices = (props.trade.tier_prices || props.trade.orders?.map((item) => item.price) || []).slice(0, 3)
  const samePrice = (left: number, right: number) => Math.abs(left - right) <= Math.max(1e-12, Math.max(Math.abs(left), Math.abs(right)) * 1e-9)
  const lineVisibility = props.lineVisibility || {}
  const isLineVisible = (key: keyof PriceLineVisibility) => lineVisibility[key] !== false
  const tierIsFilled = (price: number, index: number) => entryFills.some((fill) => fill.tier === index + 1 || samePrice(fill.price, price))
  const tierForFill = (price: number, fallbackIndex: number) => {
    const index = tierPrices.findIndex((tierPrice) => samePrice(tierPrice, price))
    return index >= 0 ? index + 1 : fallbackIndex + 1
  }
  type PriceLine = { price: number; title: string; color: string; style: LineStyle; lineWidth: 1 | 2 | 3 | 4; priority: number }
  const priceLines: PriceLine[] = []
  const addLine = (price: number | null | undefined, title: string, color: string, style = LineStyle.Dashed, lineWidth: 1 | 2 | 3 | 4 = 2, priority = 0) => {
    if (price === null || price === undefined || !Number.isFinite(price)) return
    const duplicate = priceLines.findIndex((line) => samePrice(line.price, price))
    const next = { price, title, color, style, lineWidth, priority }
    if (duplicate === -1) priceLines.push(next)
    else if (priceLines[duplicate].priority < priority) priceLines[duplicate] = next
  }
  if (isLineVisible('signal')) addLine(props.trade.signal_price, '信号', palette.signal, LineStyle.Dashed, 1, 10)
  if (isLineVisible('tiers')) tierPrices.forEach((price, index) => {
    const filled = tierIsFilled(price, index)
    addLine(price, filled ? `${entrySideLabel}${index + 1}` : `限${entrySideLabel}${index + 1}`, filled ? palette.filled : palette.pending, filled ? LineStyle.Solid : LineStyle.Dashed, 1, filled ? 50 : 40)
  })
  if (isLineVisible('average')) addLine(props.trade.average_entry_price ?? props.trade.entry_price, '开仓均价', palette.average, LineStyle.Solid, 1, 30)
  if (isLineVisible('invalid')) addLine(props.trade.invalid_price, '失效价', palette.invalid, LineStyle.Dotted, 1, 60)

  const indicatorSettings = props.indicators || {}
  if (indicatorSettings.ema) {
    const ema9 = chart.addSeries(LineSeries, { color: '#f5c451', lineWidth: 1, title: 'EMA9', priceFormat })
    const ema21 = chart.addSeries(LineSeries, { color: '#66b3ff', lineWidth: 1, title: 'EMA21', priceFormat })
    const updateEma = (nextData: ChartCandle[]) => {
      ema9.setData(lineData(nextData, emaValues(nextData, 9)))
      ema21.setData(lineData(nextData, emaValues(nextData, 21)))
    }
    updateEma(data)
    dataUpdaters.push(updateEma)
    indicatorGroups.push({
      key: 'ema', paneIndex: 0,
      values: [
        { label: 'EMA9', color: '#f5c451', series: ema9 },
        { label: 'EMA21', color: '#66b3ff', series: ema21 }
      ]
    })
  }
  let indicatorPane = 1
  if (indicatorSettings.volume) {
    const volume = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
      color: '#2ebd85'
    }, indicatorPane)
    const updateVolume = (nextData: ChartCandle[]) => volume.setData(nextData.map((bar) => ({ time: bar.time, value: bar.volume, color: bar.close >= bar.open ? '#2ebd8566' : '#f0525266' })))
    updateVolume(data)
    dataUpdaters.push(updateVolume)
    chart.priceScale('volume', indicatorPane).applyOptions({ scaleMargins: { top: 0.1, bottom: 0.05 } })
    indicatorGroups.push({ key: 'volume', paneIndex: indicatorPane, values: [{ label: 'VOL', color: '#7cc9a7', series: volume, format: 'volume' }] })
    indicatorPane += 1
  }
  if (indicatorSettings.macd) {
    const macd = chart.addSeries(LineSeries, { color: '#4da3ff', lineWidth: 1, title: 'DIF', priceFormat }, indicatorPane)
    const macdSignal = chart.addSeries(LineSeries, { color: '#f5c451', lineWidth: 1, title: 'DEA', priceFormat }, indicatorPane)
    const macdHistogram = chart.addSeries(HistogramSeries, { color: '#2ebd85', priceFormat }, indicatorPane)
    const updateMacd = (nextData: ChartCandle[]) => {
      const fast = emaValues(nextData, 12)
      const slow = emaValues(nextData, 26)
      const dif = fast.map((value, index) => value - slow[index])
      const signal = emaValues(nextData.map((bar, index) => ({ ...bar, close: dif[index] })), 9)
      const histogram = dif.map((value, index) => value - signal[index])
      macd.setData(lineData(nextData, dif))
      macdSignal.setData(lineData(nextData, signal))
      macdHistogram.setData(nextData.map((bar, index) => ({ time: bar.time, value: histogram[index], color: histogram[index] >= 0 ? '#2ebd8566' : '#f0525266' })))
    }
    updateMacd(data)
    dataUpdaters.push(updateMacd)
    indicatorGroups.push({
      key: 'macd', paneIndex: indicatorPane,
      values: [
        { label: 'DIF', color: '#4da3ff', series: macd, format: 'oscillator' },
        { label: 'DEA', color: '#f5c451', series: macdSignal, format: 'oscillator' },
        { label: 'MACD', color: '#7cc9a7', series: macdHistogram, format: 'oscillator' }
      ]
    })
    indicatorPane += 1
  }
  if (indicatorSettings.kdj) {
    const kSeries = chart.addSeries(LineSeries, { color: '#4da3ff', lineWidth: 1, title: 'K' }, indicatorPane)
    const dSeries = chart.addSeries(LineSeries, { color: '#f5c451', lineWidth: 1, title: 'D' }, indicatorPane)
    const jSeries = chart.addSeries(LineSeries, { color: '#d98bff', lineWidth: 1, title: 'J' }, indicatorPane)
    const updateKdj = (nextData: ChartCandle[]) => {
      const k: number[] = []; const d: number[] = []; const j: number[] = []
      let previousK = 50; let previousD = 50
      nextData.forEach((bar, index) => {
        const window = nextData.slice(Math.max(0, index - 8), index + 1)
        const high = Math.max(...window.map((item) => item.high)); const low = Math.min(...window.map((item) => item.low))
        const rsv = high === low ? 50 : ((bar.close - low) / (high - low)) * 100
        previousK = (2 * previousK + rsv) / 3; previousD = (2 * previousD + previousK) / 3
        k.push(previousK); d.push(previousD); j.push(3 * previousK - 2 * previousD)
      })
      kSeries.setData(lineData(nextData, k)); dSeries.setData(lineData(nextData, d)); jSeries.setData(lineData(nextData, j))
    }
    updateKdj(data)
    dataUpdaters.push(updateKdj)
    indicatorGroups.push({
      key: 'kdj', paneIndex: indicatorPane,
      values: [
        { label: 'K', color: '#4da3ff', series: kSeries, format: 'oscillator' },
        { label: 'D', color: '#f5c451', series: dSeries, format: 'oscillator' },
        { label: 'J', color: '#d98bff', series: jSeries, format: 'oscillator' }
      ]
    })
  }

  const overlayMarkers: SeriesMarker<UTCTimestamp>[] = []
  const handledOverlayKeys = new Set(['spike_high', 'invalid_price', 'tier1_price', 'tier2_price', 'tier3_price'])
  for (const overlay of props.overlays || []) {
    if (handledOverlayKeys.has(overlay.key)) continue
    const values = [props.trade.strategy_data?.[overlay.key], props.trade.attributes?.[overlay.key], props.trade.metrics?.[overlay.key], props.trade.parameters?.[overlay.key]]
    const value = values.find((item) => typeof item === 'number')
    if (isLineVisible('extensions') && overlay.kind !== 'marker' && typeof value === 'number') {
      const styles = { solid: LineStyle.Solid, dashed: LineStyle.Dashed, dotted: LineStyle.Dotted }
      const style = typeof overlay.line_style === 'number' ? overlay.line_style : styles[overlay.line_style || 'dashed']
      addLine(value, overlay.label || overlay.key, overlay.color || '#8b949e', style)
    }
    if (overlay.kind === 'marker') {
      const raw = values.find((item) => typeof item === 'object' && item !== null && !Array.isArray(item))
      const record = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : null
      const time = markerTime(record?.time as string | number | undefined)
      const price = record?.price
      if (time && typeof price === 'number') overlayMarkers.push({ time, position: 'aboveBar', color: overlay.color || '#d6a84b', shape: 'circle', text: overlay.label || overlay.key })
      else if (time) overlayMarkers.push({ time, position: 'aboveBar', color: overlay.color || '#d6a84b', shape: 'circle', text: overlay.label || overlay.key })
    }
  }

  const markers: SeriesMarker<UTCTimestamp>[] = []
  const signalTime = markerTime(props.trade.signal_time)
  const entryPosition = isShort ? 'aboveBar' : 'belowBar'
  const exitPosition = isShort ? 'belowBar' : 'aboveBar'
  const entryShape = isShort ? 'arrowDown' : 'arrowUp'
  const exitShape = isShort ? 'arrowUp' : 'arrowDown'
  const formatPrice = (value: number | undefined) => value == null ? '-' : Number(value).toFixed(pricePrecision)
  const fillCounts = new Map<'buy' | 'sell', number>()
  const displayedFillDetails = displayedFills.map((fill, index) => {
    const fillSide = String(fill.side || '').toLowerCase()
    const isBuy = fillSide === 'buy' || fillSide.includes('long')
    const sideKey = isBuy ? 'buy' : 'sell'
    const count = (fillCounts.get(sideKey) || 0) + 1
    fillCounts.set(sideKey, count)
    return {
      fill,
      label: props.fillDisplay === 'all'
        ? `${isBuy ? '买' : '卖'}${count}`
        : `${entrySideLabel}${tierForFill(fill.price, index)}`,
      position: props.fillDisplay === 'all'
        ? (isBuy ? 'belowBar' as const : 'aboveBar' as const)
        : (entryPosition as 'aboveBar' | 'belowBar'),
      shape: props.fillDisplay === 'all'
        ? (isBuy ? 'arrowUp' as const : 'arrowDown' as const)
        : (entryShape as 'arrowUp' | 'arrowDown'),
      color: props.fillDisplay === 'all'
        ? (isBuy ? palette.filled : palette.invalid)
        : palette.filled
    }
  })
  extremaPricePrecision = pricePrecision
  extremaTextColor = palette.text
  if (signalTime) markers.push({ time: signalTime, position: entryPosition, color: palette.signal, shape: 'circle', text: '信号' })
  for (const detail of displayedFillDetails) {
    const time = fillMarkerTime(detail.fill.time)
    if (time) markers.push({ time, position: detail.position, color: detail.color, shape: detail.shape, text: detail.label })
  }
  const firstFill = displayedFills[0] ?? entryFills[0]
  const entryTime = fillMarkerTime(firstFill?.time ?? props.trade.entry_time)
  if (entryTime && !markers.some((marker) => Number(marker.time) === Number(entryTime))) markers.push({ time: entryTime, position: entryPosition, color: '#1677ff', shape: entryShape, text: '首单' })
  const exitTime = fillMarkerTime(props.trade.exit_time)
  if (props.fillDisplay !== 'all' && exitTime) markers.push({ time: exitTime, position: exitPosition, color: Number(props.trade.net_pnl || 0) >= 0 ? '#2ebd85' : '#f05252', shape: exitShape, text: '退出' })
  priceLines.forEach(({ price, title, color, style, lineWidth }) => {
    series.createPriceLine({ price, title, color, lineWidth, lineStyle: style, axisLabelVisible: true })
  })
  createSeriesMarkers(series, [...markers, ...overlayMarkers].sort((a, b) => Number(a.time) - Number(b.time)))
  type EventPrice = { label: string; price: number; priority: number }
  const eventPrices = new Map<number, EventPrice[]>()
  const addEventPrice = (label: string, value: string | number | null | undefined, price: number | null | undefined, isFill = false, priority = 0) => {
    const time = isFill ? fillMarkerTime(value) : markerTime(value)
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
  const formatIndicatorValue = (value: number, format?: 'volume' | 'oscillator') => {
    if (format === 'volume') return new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 2 }).format(value)
    if (format === 'oscillator') return Number(value).toFixed(4)
    return formatPrice(value)
  }
  const handleCrosshair = (param: MouseEventParams<Time>) => {
    if (!param.point || param.time === undefined) {
      hoverLabel.value = null
      indicatorLabels.value = []
      return
    }
    const candle = param.seriesData.get(series) as { open?: number; high?: number; low?: number; close?: number } | undefined
    if (!candle || !host.value) return
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
      { label: '成交量', value: sourceCandle == null ? '-' : new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 2 }).format(sourceCandle.volume) },
      ...events.map((event) => ({ label: event.label, value: formatPrice(event.price) }))
    ]
    hoverLabel.value = {
      left: Math.min(Math.max(8, param.point.x + 12), Math.max(8, host.value.clientWidth - 210)),
      top: Math.min(Math.max(8, param.point.y + 12), Math.max(8, host.value.clientHeight - lines.length * 23 - 16)),
      lines
    }
    const hostTop = host.value.getBoundingClientRect().top
    const panes = chart?.panes() || []
    indicatorLabels.value = indicatorGroups.flatMap((group) => {
      const values = group.values.flatMap((item) => {
        const point = param.seriesData.get(item.series as ISeriesApi<SeriesType, Time>) as { value?: number } | undefined
        return typeof point?.value === 'number' ? [{ label: item.label, value: formatIndicatorValue(point.value, item.format), color: item.color }] : []
      })
      const paneTop = panes[group.paneIndex]?.getHTMLElement()?.getBoundingClientRect().top
      return values.length ? [{ key: group.key, top: paneTop == null ? 7 : paneTop - hostTop + 7, values }] : []
    })
  }
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
  const requestMore = (range: { from: number; to: number } | null) => {
    updateExtremaPoints()
    refreshExtremaLabels()
    if (!range || Date.now() < suppressEdgeRequestsUntil) return
    const visibleBars = Math.max(1, range.to - range.from)
    const prefetchBars = Math.max(20, Math.ceil(visibleBars * 3))
    const nearStart = range.from <= prefetchBars
    const nearEnd = range.to >= renderedBarTimes.length - 1 - prefetchBars
    const edge = nearStart ? 'before' : nearEnd ? 'after' : null
    if (edge && edge !== requestedEdge) {
      requestedEdge = edge
      emit('request-more', edge)
    } else if (!edge) {
      requestedEdge = null
    }
  }
  suppressEdgeRequestsUntil = Date.now() + 500
  if (preservedRange) timeScale.setVisibleRange(preservedRange)
  else timeScale.setVisibleLogicalRange({
    from: Math.max(0, focusIndex - 30),
    to: Math.min(renderedBarTimes.length - 1, focusIndex + 30)
  })
  timeScale.subscribeVisibleLogicalRangeChange(requestMore)
  unsubscribeRange = () => timeScale.unsubscribeVisibleLogicalRangeChange(requestMore)

  await nextTick()
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

function focusEvent(value: string | number | null | undefined) {
  const time = fillCandleSeconds(value)
  if (!time || !chart || !props.candles.length) return
  const data = props.candles.map((bar) => Number(seconds(bar.time) || 0)).sort((a, b) => a - b)
  if (Number(time) < data[0] || Number(time) > data[data.length - 1]) return
  let index = data.findIndex((item) => item >= Number(time))
  if (index < 0) index = data.length - 1
  suppressEdgeRequestsUntil = Date.now() + 500
  chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, index - 30), to: Math.min(data.length - 1, index + 30) })
}

function focusEntry() { focusEvent((props.trade.fills || [])[0]?.time ?? props.trade.entry_time) }
function focusExit() { focusEvent(props.trade.exit_time) }
async function resetSize() {
  const visibleRange = chart?.timeScale().getVisibleRange() ?? null
  chartHeight.value = null
  try {
    localStorage.removeItem(CHART_HEIGHT_KEY)
    localStorage.removeItem(PANE_STRETCH_KEY)
  } catch {
    // 浏览器禁用本地存储时仍可恢复本次会话的默认尺寸。
  }
  await renderChart(visibleRange)
}
defineExpose({ focusEntry, focusExit, resetSize })

function containsTime(data: ChartCandle[], value: string | number | null | undefined): boolean {
  const time = seconds(value)
  if (time === null || !data.length) return false
  return Number(time) >= Number(data[0].time) && Number(time) <= Number(data[data.length - 1].time)
}

async function updateChartData() {
  if (!chart || !dataUpdaters.length || !props.candles.length) {
    await renderChart()
    return
  }
  const nextData = normalizeCandles(props.candles)
  const visibleRange = chart.timeScale().getVisibleRange()
  const focusArrived = !containsTime(renderedCandles, props.focusTime) && containsTime(nextData, props.focusTime)
  const eventTimes = [
    props.trade.signal_time,
    fillCandleSeconds(props.trade.entry_time),
    fillCandleSeconds(props.trade.exit_time),
    ...(props.trade.fills || []).map((fill) => fillCandleSeconds(fill.time))
  ]
  const markerArrived = eventTimes.some((time) => !containsTime(renderedCandles, time) && containsTime(nextData, time))
  if (focusArrived || markerArrived) {
    await renderChart(focusArrived ? null : visibleRange)
    return
  }
  renderedCandles = nextData
  renderedBarTimes = nextData.map((bar) => Number(bar.time))
  renderedCandleByTime = new Map(nextData.map((bar) => [Number(bar.time), bar]))
  dataUpdaters.forEach((update) => update(nextData))
  updateExtremaPoints()
  refreshExtremaLabels()
  if (visibleRange) {
    chart.timeScale().setVisibleRange(visibleRange)
  }
  requestedEdge = null
}

watch(() => props.candles, updateChartData, { deep: true })
watch(() => [props.trade, props.overlays, props.indicators, props.lineVisibility, props.focusTime], () => renderChart(), { immediate: true, deep: true })
watch(isDarkTheme, () => renderChart())
onBeforeUnmount(() => {
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
  border: 1px solid var(--chart-border);
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
  border: 1px solid var(--chart-grid);
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
