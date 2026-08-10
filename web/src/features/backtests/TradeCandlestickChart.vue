<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import {
  CandlestickSeries,
  ColorType,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type SeriesMarker,
  type UTCTimestamp
} from 'lightweight-charts'
import type { BacktestCandle, BacktestTradeDetail, ChartOverlay } from '@/api/types'
import { timestampMs } from './format'

const props = defineProps<{
  candles: BacktestCandle[]
  trade: BacktestTradeDetail
  overlays?: ChartOverlay[]
}>()

const host = ref<HTMLElement | null>(null)
let chart: IChartApi | null = null
let observer: ResizeObserver | null = null

function seconds(value: string | number | null | undefined): UTCTimestamp | null {
  const ms = timestampMs(value)
  return ms === null ? null : Math.floor(ms / 1000) as UTCTimestamp
}

function destroy() {
  observer?.disconnect()
  observer = null
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
    crosshair: { vertLine: { color: '#73808f' }, horzLine: { color: '#73808f' } },
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
      if (time && typeof price === 'number') overlayMarkers.push({ time, position: 'atPriceTop', price, color: overlay.color || '#d6a84b', shape: 'circle', text: overlay.label || overlay.key })
      else if (time) overlayMarkers.push({ time, position: 'aboveBar', color: overlay.color || '#d6a84b', shape: 'circle', text: overlay.label || overlay.key })
    }
  }

  const markers: SeriesMarker<UTCTimestamp>[] = []
  const signalTime = markerTime(props.trade.signal_time)
  if (signalTime && props.trade.signal_price != null) markers.push({ time: signalTime, position: 'atPriceTop', price: props.trade.signal_price, color: '#e0a526', shape: 'circle', text: '信号' })
  else if (signalTime) markers.push({ time: signalTime, position: 'aboveBar', color: '#e0a526', shape: 'circle', text: '信号' })
  for (const fill of props.trade.fills || []) {
    const time = markerTime(fill.time)
    if (time) markers.push({ time, position: 'atPriceTop', price: fill.price, color: '#f2f4f7', shape: 'arrowDown', text: `成交${fill.tier ? ` T${fill.tier}` : ''} ${fill.price}` })
  }
  const entryTime = markerTime(props.trade.entry_time)
  if (entryTime && !(props.trade.fills?.length)) markers.push({ time: entryTime, position: 'aboveBar', color: '#f2f4f7', shape: 'arrowDown', text: '开仓' })
  const exitTime = markerTime(props.trade.exit_time)
  if (exitTime && props.trade.exit_price != null) markers.push({ time: exitTime, position: 'atPriceBottom', price: props.trade.exit_price, color: props.trade.net_pnl >= 0 ? '#2ebd85' : '#f05252', shape: 'arrowUp', text: `退出 ${props.trade.exit_reason || ''}` })
  else if (exitTime) markers.push({ time: exitTime, position: 'belowBar', color: props.trade.net_pnl >= 0 ? '#2ebd85' : '#f05252', shape: 'arrowUp', text: `退出 ${props.trade.exit_reason || ''}` })
  createSeriesMarkers(series, [...markers, ...overlayMarkers].sort((a, b) => Number(a.time) - Number(b.time)))
  chart.timeScale().fitContent()

  observer = new ResizeObserver((entries) => {
    const width = entries[0]?.contentRect.width
    if (width && chart) chart.applyOptions({ width })
  })
  observer.observe(host.value)
}

watch(() => [props.candles, props.trade, props.overlays], renderChart, { immediate: true, deep: true })
onBeforeUnmount(destroy)
</script>

<template><div ref="host" class="candlestick-host" /></template>
