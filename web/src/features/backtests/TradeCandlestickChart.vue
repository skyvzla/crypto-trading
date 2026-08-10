<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
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
}>()
const emit = defineEmits<{ 'request-more': [direction: 'before' | 'after'] }>()

const host = ref<HTMLElement | null>(null)
let chart: IChartApi | null = null
let observer: ResizeObserver | null = null
let unsubscribeRange: (() => void) | null = null
let unsubscribeCrosshair: (() => void) | null = null
let requestedEdge: 'before' | 'after' | null = null
const hoverLabel = ref<{ left: number; top: number; text: string } | null>(null)

function seconds(value: string | number | null | undefined): UTCTimestamp | null {
  const ms = timestampMs(value)
  return ms === null ? null : Math.floor(ms / 1000) as UTCTimestamp
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
  chart?.remove()
  chart = null
}

async function renderChart() {
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
  const data = props.candles
    .map((bar) => ({ ...bar, time: (bar.time > 10_000_000_000 ? Math.floor(bar.time / 1000) : bar.time) as UTCTimestamp }))
    .sort((a, b) => Number(a.time) - Number(b.time))
  series.setData(data)
  const barTimes = data.map((bar) => Number(bar.time))
  const markerTime = (value: string | number | null | undefined): UTCTimestamp | null => {
    const exact = seconds(value)
    if (exact === null || !barTimes.length) return null
    const intervalSeconds = barTimes.length > 1
      ? barTimes[1] - barTimes[0]
      : 60
    if (exact < barTimes[0] || exact > barTimes[barTimes.length - 1] + intervalSeconds) return null
    let low = 0
    let high = barTimes.length - 1
    let matched = barTimes[0]
    while (low <= high) {
      const middle = Math.floor((low + high) / 2)
      if (barTimes[middle] <= exact) {
        matched = barTimes[middle]
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
  for (const fill of props.trade.fills || []) {
    const time = markerTime(fill.time)
    if (time) markers.push({ time, position: entryPosition, color: '#1677ff', shape: entryShape, text: `成交${fill.tier ? ` T${fill.tier}` : ''}` })
  }
  const entryTime = markerTime(props.trade.entry_time)
  if (entryTime && !(props.trade.fills?.length)) markers.push({ time: entryTime, position: entryPosition, color: '#1677ff', shape: entryShape, text: '开仓' })
  const exitTime = markerTime(props.trade.exit_time)
  if (exitTime) markers.push({ time: exitTime, position: exitPosition, color: props.trade.net_pnl >= 0 ? '#2ebd85' : '#f05252', shape: exitShape, text: '退出' })
  createSeriesMarkers(series, [...markers, ...overlayMarkers].sort((a, b) => Number(a.time) - Number(b.time)))
  const eventPrices = new Map<number, number[]>()
  const addEventPrice = (value: string | number | null | undefined, price: number | null | undefined) => {
    const time = seconds(value)
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
  const focusTimes = [seconds(props.trade.signal_time), seconds(props.trade.entry_time), seconds(props.trade.exit_time)]
    .filter((time): time is UTCTimestamp => time !== null)
  const focusTime = focusTimes.length
    ? focusTimes.reduce((sum, time) => sum + Number(time), 0) / focusTimes.length
    : Number(barTimes[Math.floor(barTimes.length / 2)])
  let focusIndex = barTimes.findIndex((time) => time >= focusTime)
  if (focusIndex < 0) focusIndex = barTimes.length - 1
  const timeScale = chart.timeScale()
  chart.subscribeCrosshairMove(handleCrosshair)
  unsubscribeCrosshair = () => chart?.unsubscribeCrosshairMove(handleCrosshair)
  const requestMore = (range: { from: number; to: number } | null) => {
    if (!range) return
    const nearStart = range.from < 80
    const nearEnd = range.to > barTimes.length - 80
    const edge = nearStart ? 'before' : nearEnd ? 'after' : null
    if (edge && edge !== requestedEdge) {
      requestedEdge = edge
      emit('request-more', edge)
    } else if (!edge) {
      requestedEdge = null
    }
  }
  timeScale.subscribeVisibleLogicalRangeChange(requestMore)
  unsubscribeRange = () => timeScale.unsubscribeVisibleLogicalRangeChange(requestMore)
  timeScale.setVisibleLogicalRange({
    from: Math.max(0, focusIndex - 30),
    to: Math.min(barTimes.length - 1, focusIndex + 30)
  })

  observer = new ResizeObserver((entries) => {
    const width = entries[0]?.contentRect.width
    if (width && chart) chart.applyOptions({ width })
  })
  observer.observe(host.value)
}

watch(() => [props.candles, props.trade, props.overlays], renderChart, { immediate: true, deep: true })
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
  border: 1px solid #d0d5dd;
  border-radius: 4px;
  background: rgba(255, 255, 255, .94);
  color: #344054;
  font: 11px/1.4 "JetBrains Mono", monospace;
  pointer-events: none;
  white-space: nowrap;
}
</style>
