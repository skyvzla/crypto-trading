<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
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
  type MouseEventParams,
  type SeriesMarker,
  type Time,
  type UTCTimestamp
} from 'lightweight-charts'
import type { BacktestCandle, BacktestTradeDetail, ChartOverlay } from '@/api/types'
import { timestampMs } from './format'

const props = defineProps<{
  candles: BacktestCandle[]
  trade: BacktestTradeDetail
  overlays?: ChartOverlay[]
  focusTime?: string | number | null
  indicators?: {
    volume?: boolean
    macd?: boolean
    ema?: boolean
    kdj?: boolean
  }
}>()
const emit = defineEmits<{ 'request-more': [direction: 'before' | 'after'] }>()

const host = ref<HTMLElement | null>(null)
let chart: IChartApi | null = null
let observer: ResizeObserver | null = null
let unsubscribeRange: (() => void) | null = null
let unsubscribeCrosshair: (() => void) | null = null
let requestedEdge: 'before' | 'after' | null = null
const hoverLabel = ref<{ left: number; top: number; text: string } | null>(null)
type ChartCandle = Omit<BacktestCandle, 'time'> & { time: UTCTimestamp }
let renderedCandles: ChartCandle[] = []
let renderedBarTimes: number[] = []
let dataUpdaters: Array<(data: ChartCandle[]) => void> = []

function seconds(value: string | number | null | undefined): UTCTimestamp | null {
  const ms = timestampMs(value)
  return ms === null ? null : Math.floor(ms / 1000) as UTCTimestamp
}

function normalizeCandles(candles: BacktestCandle[]): ChartCandle[] {
  return candles
    .map((bar) => ({ ...bar, time: (bar.time > 10_000_000_000 ? Math.floor(bar.time / 1000) : bar.time) as UTCTimestamp }))
    .sort((a, b) => Number(a.time) - Number(b.time))
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

function destroy() {
  observer?.disconnect()
  observer = null
  unsubscribeRange?.()
  unsubscribeRange = null
  unsubscribeCrosshair?.()
  unsubscribeCrosshair = null
  hoverLabel.value = null
  requestedEdge = null
  renderedCandles = []
  renderedBarTimes = []
  dataUpdaters = []
  chart?.remove()
  chart = null
}

async function renderChart(preservedRange: { from: Time; to: Time } | null = null) {
  destroy()
  await nextTick()
  if (!host.value || !props.candles.length) return
  chart = createChart(host.value, {
    width: host.value.clientWidth,
    height: Math.max(420, host.value.clientHeight),
    layout: { background: { type: ColorType.Solid, color: '#0d1117' }, textColor: '#9ca8b8', attributionLogo: true },
    grid: { vertLines: { color: '#1c242d' }, horzLines: { color: '#1c242d' } },
    rightPriceScale: { borderColor: '#303944' },
    timeScale: { borderColor: '#303944', timeVisible: true, secondsVisible: false },
    crosshair: { mode: CrosshairMode.Normal, vertLine: { color: '#73808f' }, horzLine: { color: '#73808f' } },
    localization: { locale: 'zh-CN' }
  })
  const series = chart.addSeries(CandlestickSeries, {
    upColor: '#2ebd85', downColor: '#f05252', borderVisible: false,
    wickUpColor: '#2ebd85', wickDownColor: '#f05252'
  })
  const data = normalizeCandles(props.candles)
  renderedCandles = data
  renderedBarTimes = data.map((bar) => Number(bar.time))
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

  const addLine = (price: number | null | undefined, title: string, color: string, style = 2) => {
    if (price === null || price === undefined || !Number.isFinite(price)) return
    series.createPriceLine({ price, title, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true })
  }
  addLine(props.trade.signal_price, '信号', '#e0a526')
  ;(props.trade.tier_prices || props.trade.orders?.map((item) => item.price) || []).slice(0, 3)
    .forEach((price, index) => addLine(price, `挂单 ${index + 1}`, ['#65a7c9', '#a58bd4', '#d98b5f'][index]))
  addLine(props.trade.average_entry_price ?? props.trade.entry_price, '开仓均价', '#f2f4f7', 0)
  addLine(props.trade.invalid_price, '失效价', '#f05252', 0)

  const indicatorSettings = props.indicators || {}
  if (indicatorSettings.ema) {
    const ema9 = chart.addSeries(LineSeries, { color: '#f5c451', lineWidth: 1, title: 'EMA9' })
    const ema21 = chart.addSeries(LineSeries, { color: '#66b3ff', lineWidth: 1, title: 'EMA21' })
    const updateEma = (nextData: ChartCandle[]) => {
      ema9.setData(lineData(nextData, emaValues(nextData, 9)))
      ema21.setData(lineData(nextData, emaValues(nextData, 21)))
    }
    updateEma(data)
    dataUpdaters.push(updateEma)
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
    indicatorPane += 1
  }
  if (indicatorSettings.macd) {
    const macd = chart.addSeries(LineSeries, { color: '#4da3ff', lineWidth: 1, title: 'DIF' }, indicatorPane)
    const macdSignal = chart.addSeries(LineSeries, { color: '#f5c451', lineWidth: 1, title: 'DEA' }, indicatorPane)
    const macdHistogram = chart.addSeries(HistogramSeries, { color: '#2ebd85' }, indicatorPane)
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
  }

  const overlayMarkers: SeriesMarker<UTCTimestamp>[] = []
  for (const overlay of props.overlays || []) {
    const values = [props.trade.strategy_data?.[overlay.key], props.trade.attributes?.[overlay.key], props.trade.metrics?.[overlay.key], props.trade.parameters?.[overlay.key]]
    const value = values.find((item) => typeof item === 'number')
    if (overlay.kind !== 'marker' && typeof value === 'number') {
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
  const isShort = String(props.trade.side || '').toLowerCase().includes('short') || String(props.trade.side || '').toLowerCase() === 'sell'
  const entryPosition = isShort ? 'aboveBar' : 'belowBar'
  const exitPosition = isShort ? 'belowBar' : 'aboveBar'
  const entryShape = isShort ? 'arrowDown' : 'arrowUp'
  const exitShape = isShort ? 'arrowUp' : 'arrowDown'
  if (signalTime) markers.push({ time: signalTime, position: entryPosition, color: '#e0a526', shape: 'circle', text: '信号' })
  const fills = props.trade.fills || []
  for (const fill of fills) {
    const time = markerTime(fill.time)
    if (time) markers.push({ time, position: entryPosition, color: '#1677ff', shape: entryShape, text: `成交${fill.tier ? ` T${fill.tier}` : ''}` })
  }
  const firstFill = fills[0]
  const entryTime = markerTime(firstFill?.time ?? props.trade.entry_time)
  if (entryTime && !markers.some((marker) => Number(marker.time) === Number(entryTime))) markers.push({ time: entryTime, position: entryPosition, color: '#1677ff', shape: entryShape, text: '首单' })
  const exitTime = markerTime(props.trade.exit_time)
  if (exitTime) markers.push({ time: exitTime, position: exitPosition, color: props.trade.net_pnl >= 0 ? '#2ebd85' : '#f05252', shape: exitShape, text: '退出' })
  createSeriesMarkers(series, [...markers, ...overlayMarkers].sort((a, b) => Number(a.time) - Number(b.time)))
  const eventPrices = new Map<number, number[]>()
  const addEventPrice = (value: string | number | null | undefined, price: number | null | undefined) => {
    const time = markerTime(value)
    if (time === null || typeof price !== 'number') return
    eventPrices.set(Number(time), [...(eventPrices.get(Number(time)) || []), price])
  }
  for (const fill of props.trade.fills || []) addEventPrice(fill.time, fill.price)
  addEventPrice(props.trade.entry_time, props.trade.average_entry_price ?? props.trade.entry_price)
  addEventPrice(props.trade.exit_time, props.trade.exit_price)
  const formatPrice = (value: number | undefined) => value == null ? '-' : Number(value).toPrecision(8)
  const handleCrosshair = (param: MouseEventParams<Time>) => {
    if (!param.point || param.time === undefined) {
      hoverLabel.value = null
      return
    }
    const candle = param.seriesData.get(series) as { open?: number; high?: number; low?: number; close?: number } | undefined
    if (!candle || !host.value) return
    const prices = eventPrices.get(Number(param.time)) || []
    const eventText = prices.length ? `  事件价 ${prices.map(formatPrice).join(' / ')}` : ''
    hoverLabel.value = {
      left: Math.min(Math.max(8, param.point.x + 12), Math.max(8, host.value.clientWidth - 260)),
      top: Math.max(8, param.point.y - 48),
      text: `收 ${formatPrice(candle.close)} O${formatPrice(candle.open)} H${formatPrice(candle.high)} L${formatPrice(candle.low)}${eventText}`
    }
  }
  const initialFocusTime = Number(seconds(props.focusTime ?? firstFill?.time ?? props.trade.entry_time ?? props.trade.signal_time) ?? renderedBarTimes[Math.floor(renderedBarTimes.length / 2)])
  let focusIndex = renderedBarTimes.findIndex((time) => time >= initialFocusTime)
  if (focusIndex < 0) focusIndex = renderedBarTimes.length - 1
  const timeScale = chart.timeScale()
  chart.subscribeCrosshairMove(handleCrosshair)
  unsubscribeCrosshair = () => chart?.unsubscribeCrosshairMove(handleCrosshair)
  const requestMore = (range: { from: number; to: number } | null) => {
    if (!range) return
    const nearStart = range.from < 80
    const nearEnd = range.to > renderedBarTimes.length - 80
    const edge = nearStart ? 'before' : nearEnd ? 'after' : null
    if (edge && edge !== requestedEdge) {
      requestedEdge = edge
      emit('request-more', edge)
    } else if (!edge) {
      requestedEdge = null
    }
  }
  if (preservedRange) timeScale.setVisibleRange(preservedRange)
  else timeScale.setVisibleLogicalRange({
    from: Math.max(0, focusIndex - 30),
    to: Math.min(renderedBarTimes.length - 1, focusIndex + 30)
  })
  timeScale.subscribeVisibleLogicalRangeChange(requestMore)
  unsubscribeRange = () => timeScale.unsubscribeVisibleLogicalRangeChange(requestMore)

  observer = new ResizeObserver((entries) => {
    const width = entries[0]?.contentRect.width
    if (width && chart) chart.applyOptions({ width })
  })
  observer.observe(host.value)
}

function focusEvent(value: string | number | null | undefined) {
  const time = seconds(value)
  if (!time || !chart || !props.candles.length) return
  const data = props.candles.map((bar) => Number(seconds(bar.time) || 0)).sort((a, b) => a - b)
  if (Number(time) < data[0] || Number(time) > data[data.length - 1]) return
  let index = data.findIndex((item) => item >= Number(time))
  if (index < 0) index = data.length - 1
  chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, index - 30), to: Math.min(data.length - 1, index + 30) })
}

function focusEntry() { focusEvent((props.trade.fills || [])[0]?.time ?? props.trade.entry_time) }
function focusExit() { focusEvent(props.trade.exit_time) }
defineExpose({ focusEntry, focusExit })

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
  const eventTimes = [props.trade.signal_time, props.trade.entry_time, props.trade.exit_time, ...(props.trade.fills || []).map((fill) => fill.time)]
  const markerArrived = eventTimes.some((time) => !containsTime(renderedCandles, time) && containsTime(nextData, time))
  if (focusArrived || markerArrived) {
    await renderChart(focusArrived ? null : visibleRange)
    return
  }
  renderedCandles = nextData
  renderedBarTimes = nextData.map((bar) => Number(bar.time))
  dataUpdaters.forEach((update) => update(nextData))
  if (visibleRange) chart.timeScale().setVisibleRange(visibleRange)
}

watch(() => props.candles, updateChartData, { deep: true })
watch(() => [props.trade, props.overlays, props.indicators, props.focusTime], () => renderChart(), { immediate: true, deep: true })
onBeforeUnmount(destroy)
</script>

<template>
  <div ref="host" class="candlestick-host">
    <div v-if="hoverLabel" class="chart-hover-label" :style="{ left: `${hoverLabel.left}px`, top: `${hoverLabel.top}px` }">{{ hoverLabel.text }}</div>
  </div>
</template>

<style scoped>
.candlestick-host { position: relative; }
.chart-hover-label {
  position: absolute;
  z-index: 2;
  max-width: calc(100% - 16px);
  padding: 5px 8px;
  border: 1px solid #354352;
  border-radius: 4px;
  background: rgba(17, 24, 32, .96);
  color: #d7e0ea;
  font: 11px/1.4 "JetBrains Mono", monospace;
  pointer-events: none;
  white-space: nowrap;
}
</style>
